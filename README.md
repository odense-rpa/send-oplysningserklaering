# Send oplysningserklæring

Robotten sender automatisk oplysningserklæringer til borgere som digital post, når de indsender relevante skemaer i KMD Nexus.

## Hvad gør robotten?

1. Læser aktivitetslisten *Robot: skemaer der udløser afsendelse af oplysningserklæringer* i KMD Nexus og fylder arbejdskøen med nye skemaer (maks. 5 dage gamle, springer allerede behandlede over).
2. Henter borgerens CPR-nummer fra skemadata og verificerer via Datafordeler, at borgeren er i live.
3. Slår borgeren op i KMD Nexus og sikrer, at grundforløbet *Oplysningserklæringer* eksisterer.
4. Tjekker om der allerede ligger en gyldig oplysningserklæring (under 1 år gammel) for samme skematype i borgerens forløb — springer over, hvis det er tilfældet.
5. Finder den matchende brevskabelon og brevtype i Excel-filen `Regler.xlsx` ud fra skemaets navn (Henvendelsestype).
6. Konverterer Word-skabelonen (.docx) til PDF via en intern HTTP-konverteringstjeneste.
7. Sender PDF-filen som digital post til borgeren via SBSip med emnet *Sådan behandler vi dine personoplysninger*.
8. Uploader den afsendte PDF til borgerens *Oplysningserklæringer*-forløb i KMD Nexus.
9. Registrerer den afsluttede opgave i SQL-sporingsdatabasen og sletter den lokale PDF-fil.

## Forudsætninger

- Python ≥ 3.13
- [`uv`](https://docs.astral.sh/uv/) til pakkehåndtering
- Adgang til **Automation Server** (arbejdskø og credentials)
- Adgang til **KMD Nexus** (produktion)
- Adgang til **SBSip** (digital post, produktion)
- Adgang til **Odense SQL Server** (sporingsdatabase)
- Adgang til **Datafordeler** (inkl. klientcertifikater)
- Word-til-PDF-konverteringstjeneste kørende på `http://rpa-ats.odknet.dk:8331/convert`

## Installation

```sh
uv sync
```

## Konfiguration

Credentials registreres i Automation Server:

- `KMD Nexus - produktion`
- `Odense SQL Server`
- `SBSip - produktion`

Derudover kræves følgende miljøvariabler:

| Variabel | Beskrivelse |
|---|---|
| `ATS_URL` | URL til Automation Server API |
| `ATS_TOKEN` | Bearer token til Automation Server |
| `ATS_WORKQUEUE_OVERRIDE` | Tilsidesæt arbejdskø-ID (valgfri) |
| `CERTIFIKATER` | Sti til mappe med Datafordeler TLS-certifikater (`datafordeler.crt` og `datafordeler.key`) — standard: `/certifikater` |

Word-skabeloner placeres i mappen `Send oplysningserklæring` (kan tilpasses med `--word-template-path`). Mapping fra skemanavne til brevskabeloner defineres i `Regler.xlsx`.

## Kørsel

```sh
uv run python main.py --queue   # Fyld arbejdskøen
uv run python main.py           # Behandl arbejdskøen
```

## Afhængigheder

| Pakke | Formål |
|---|---|
| `automation-server-client` | Arbejdskøstyring, credential-hentning og Automation Server-integration |
| `datafordeler` | Opslag af borgerdata og adresse via Datafordeler med certifikatbaseret autentificering |
| `odk-tools` | Opgavesporing via Tracker-hjælpeprogrammet |
| `kmd-nexus-client` | Interaktion med KMD Nexus: borgerregistre, forløb, aktivitetslister og dokumentupload |
| `openpyxl` | Læsning af Excel-regelfilen `Regler.xlsx` |
| `sbsip` | Afsendelse af digital post (PDF-vedhæftninger) til borgere |
| `ruff` | Kode-linting |

## GDPR og sikkerhed

Robotten behandler følgende personoplysninger: CPR-numre, borgeradresser samt dokumenter tilknyttet borgernes sundheds- og socialforløb. Oplysningserklæringer gemmes i borgerens forløb i KMD Nexus og i SQL-sporingsdatabasen. Adgang til disse systemer styres via Automation Server-credentials og kræver de rette rollerettigheder i de respektive systemer.
