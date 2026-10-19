import argparse
import asyncio
import httpx
import logging
import sys
import os
import sbsip

from automation_server_client import (
    AutomationServer,
    Workqueue,
    WorkItemError,
    Credential,
    WorkItemStatus,
)
from datafordeler import DatafordelerGraphQL
from datetime import datetime, timedelta
from kmd_nexus_client import NexusClientManager
from kmd_nexus_client.utils import sanitize_cpr
from kmd_nexus_client.tree_helpers import filter_by_path, filter_by_predicate
from odk_tools.tracking import Tracker
from pathlib import Path
from process.config import load_excel_mapping, get_excel_mapping

datafordeler: DatafordelerGraphQL
proces_navn = "Send oplysningserklæring"


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not isinstance(value, str):
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _hent_borger_oplysninger(data: dict):
    """Hent borgeroplysninger fra datafordeler og Nexus."""
    cpr = sanitize_cpr(data["patients"][0].get("patientIdentifier").get("identifier"))

    if datafordeler.hent_person(str(cpr)).er_død:
        return None

    borger = nexus.borgere.hent_borger(borger_cpr=cpr)
    if not borger:
        return None

    return cpr, borger


def _sikre_forloeb_og_hent_regel(borger, data: dict, regler: list):
    """Sikre grundforløb i Nexus og hent referencer til dokumenter i forløbet."""
    nexus.forløb.opret_forløb(
        borger=borger,
        grundforløb_navn="Oplysningserklæringer",
    )

    visning = nexus.borgere.hent_visning(borger)
    referencer = nexus.borgere.hent_referencer(visning)
    referencer = filter_by_path(
        roots=referencer,
        path_pattern="/Oplysningserklæringer/*",
        active_pathways_only=True,
    )

    skema_navn = data.get("name")
    pdf_filer = [
        ref
        for ref in filter_by_predicate(
            referencer,
            lambda node: (
                isinstance(node.get("name"), str)
                and node["name"].lower().endswith(".pdf")
            ),
        )
        if (
            (pdf_dato := _parse_iso_datetime(ref.get("date")))
            and (
                pdf_dato.date() + timedelta(days=365)
                > datetime.now(pdf_dato.tzinfo).date()
            )
            and isinstance(skema_navn, str)
            and (skema_navn.lower() in ref["name"].lower())
        )
    ]

    if pdf_filer:
        return None

    regel = next(
        (
            r
            for r in regler
            if isinstance(skema_navn, str)
            and isinstance(r.get("Henvendelsestype"), str)
            and r["Henvendelsestype"].strip().lower() == skema_navn.strip().lower()
        ),
        None,
    )

    if not regel:
        return None

    return visning, regel


def _konverter_og_send_digital_post(
    cpr: str, regel: dict, word_template_path: str
) -> Path:
    """Send digital post til borger med oplysningserklæring via SBSip."""
    with open(f"{word_template_path}/{regel['Brevskabelon']}.docx", "rb") as f:
        response = httpx.post(
            "http://rpa-ats.odknet.dk:8331/convert",
            files={"file": (f"{word_template_path}/{regel['Brevskabelon']}.docx", f)},
        )

    pdf_path = Path(
        f"{regel['Brevskabelon']} {datetime.now().strftime('%d-%m-%Y')}.pdf"
    )
    pdf_path.write_bytes(response.content)

    adresse, post_nr = datafordeler.hent_person(cpr).sbsip_adresse()

    sbsip.send_digital_post(
        cpr=cpr,
        overskrift="Sådan behandler vi dine personoplysninger",
        beskrivelse="",
        vedhæftet_fil=pdf_path,
        adresse=adresse,
        post_nr=post_nr
    )

    return pdf_path


def _upload_til_nexus(borger, visning, regel: dict, pdf_path: Path):
    """Upload dokument til Nexus."""
    referencer = nexus.borgere.hent_referencer(visning)
    grundforløb = filter_by_path(
        roots=referencer,
        path_pattern="/Oplysningserklæringer",
        active_pathways_only=True,
    )

    grundforløb = nexus.hent_fra_reference(grundforløb[0])

    nexus.forløb.opret_dokument(
        borger=borger,
        forløb=grundforløb,
        fil=pdf_path.read_bytes(),
        filnavn=f"Oplysningserklæring - {regel['Henvendelsestype']} {datetime.now().strftime('%d-%m-%Y')}.pdf",
        titel=f"Oplysningserklæring - {regel['Henvendelsestype']} {datetime.now().strftime('%d-%m-%Y')}",
        noter=None,
        modtaget=datetime.now(),
    )

    pdf_path.unlink()
    tracker.track_task(proces_navn)


async def populate_queue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)

    aktivitetsliste = nexus.aktivitetslister.hent_aktivitetsliste(
        navn="Robot: skemaer der udløser afsendelse af oplysningserklæringer",
        organisation=None,
        medarbejder=None,
        antal_sider=20,
    )

    if aktivitetsliste:
        for aktivitet in aktivitetsliste:
            eksisterende_kødata = workqueue.get_item_by_reference(str(aktivitet["id"]))

            if len(eksisterende_kødata) > 0:
                continue

            skema = nexus.nexus_client.get(
                aktivitet.get("_links").get("referencedObject").get("href")
            ).json()

            create_date_raw = skema.get("createDate")
            if not isinstance(create_date_raw, str):
                logger.warning(
                    "Skipping aktivitet %s: missing or invalid createDate",
                    aktivitet.get("id"),
                )
                continue

            # Nexus often returns ISO timestamps with trailing "Z" (UTC).
            try:
                create_date = _parse_iso_datetime(create_date_raw)
            except ValueError:
                logger.warning(
                    "Skipping aktivitet %s: unparseable createDate '%s'",
                    aktivitet.get("id"),
                    create_date_raw,
                )
                continue

            if create_date < datetime.now(create_date.tzinfo) - timedelta(days=5):
                continue

            workqueue.add_item(aktivitet, str(aktivitet["id"]))


async def process_workqueue(workqueue: Workqueue):
    logger = logging.getLogger(__name__)
    regler = get_excel_mapping()

    for item in workqueue:
        with item:
            data = item.data

            try:
                resultat = _hent_borger_oplysninger(data)
                if resultat is None:
                    continue
                cpr, borger = resultat

                resultat = _sikre_forloeb_og_hent_regel(borger, data, regler)
                if resultat is None:
                    continue
                visning, regel = resultat

                pdf_path = _konverter_og_send_digital_post(
                    cpr, regel, args.word_template_path
                )

                _upload_til_nexus(borger, visning, regel, pdf_path)

            except WorkItemError as e:
                logger.error(f"Error processing item: {data}. Error: {e}")
                item.fail(str(e))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    ats = AutomationServer.from_environment()
    workqueue = ats.workqueue()

    nexus_credential = Credential.get_credential("KMD Nexus - produktion")
    tracking_credential = Credential.get_credential("Odense SQL Server")
    SBSip_credential = Credential.get_credential("SBSip - produktion")
    datafordeler_credential = Credential.get_credential("Datafordeler")

    datafordeler = DatafordelerGraphQL(
        client_id=datafordeler_credential.username,
        client_secret=datafordeler_credential.password,
    )

    nexus = NexusClientManager(
        client_id=nexus_credential.username,
        client_secret=nexus_credential.password,
        instance=nexus_credential.data["instance"],
        timeout=120,
    )

    tracker = Tracker(
        username=tracking_credential.username, password=tracking_credential.password
    )

    sbsip.start_sbsip(
        brugernavn=SBSip_credential.username,
        adgangskode=SBSip_credential.password,
    )

    # Parse command line arguments
    parser = argparse.ArgumentParser(description=proces_navn)
    parser.add_argument(
        "--excel-file",
        default="./Regler.xlsx",
        help="Path to the Excel file containing mapping data (default: ./Regler.xlsx)",
    )
    parser.add_argument(
        "--queue",
        action="store_true",
        help="Populate the queue with test data and exit",
    )
    parser.add_argument("--word-template-path", default="./Send oplysningserklæring")
    args = parser.parse_args()

    # Validate Excel file exists
    if not os.path.isfile(args.excel_file):
        raise FileNotFoundError(f"Excel file not found: {args.excel_file}")

    # Load excel mapping data once on startup
    load_excel_mapping(args.excel_file)

    # Queue management
    if "--queue" in sys.argv:
        workqueue.clear_workqueue(WorkItemStatus.NEW)
        asyncio.run(populate_queue(workqueue))
        exit(0)

    # Process workqueue
    asyncio.run(process_workqueue(workqueue))
