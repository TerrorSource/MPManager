# MPManager

Een webpagina in Docker waarmee je je Marktplaats-advertenties bekijkt, backupt
en opnieuw plaatst. Werkt volledig met gewone HTTP-verzoeken: geen browser, geen
Playwright.

## Aan de slag

### 1. Drie mappen op je NAS

Eén voor de login en instellingen, één voor de backups, één voor de sleutel:

```
/share/CACHEDEV1_DATA/Docker/mpmanager/config
/share/CACHEDEV1_DATA/Docker/mpmanager/backups
/share/CACHEDEV1_DATA/Docker/mpmanager/keys
```

### 2. Start de container

In Portainer: *Stacks* → *Add stack* → *Web editor*, en plak `docker-compose.yml`
uit deze repo. De korte versie, met alles wat je moet aanpassen:

```yaml
version: "3"

services:
  mpmanager:
    container_name: mpmanager
    image: ghcr.io/terrorsource/mpmanager:latest
    init: true
    restart: unless-stopped
    network_mode: bridge
    environment:
      - TZ=Europe/Amsterdam
      - PUID=1000                         # draai als deze gebruiker, zodat de bestanden van jou zijn
      - PGID=1000
      - WEB_USER=gebruikersnaam           # login voor de pagina: wijzig beide
      - WEB_PASSWORD=password             # deze voorbeeldwaarde wordt geweigerd
      - DRY_RUN=true                      # alleen de beginwaarde; daarna geldt Instellingen
    ports:
      - 8096:8080
    volumes:
      - /share/CACHEDEV1_DATA/Docker/mpmanager/config:/config
      - /share/CACHEDEV1_DATA/Docker/mpmanager/backups:/data/backups
      - /share/CACHEDEV1_DATA/Docker/mpmanager/keys:/keys
```

- `WEB_USER` / `WEB_PASSWORD`: de pagina kan je advertenties verwijderen en je
  typt er je Marktplaats-wachtwoord in, dus kies iets echts. Staat het
  wachtwoord nog op `password`, dan toont de pagina een melding en komt niemand
  binnen. Laat je `WEB_PASSWORD` helemaal weg, dan heeft de pagina geen login.
- `PUID` / `PGID`: de gebruiker waaronder de container draait, zodat de
  bestanden op de NAS van jou zijn. Laat beide weg om als root te draaien.
- De drie mappen: die uit stap 1.

De volledige `docker-compose.yml` bevat ook de beginwaarden van alle
instellingen (daglimiet, actieve uren, enzovoort). Die pas je later op de
pagina aan, dus weglaten kan.

Deploy en open `http://<nas-ip>:8096`. Het is een gewone inlogpagina, dus je
browser of wachtwoordmanager kan hem onthouden.

Zonder Portainer: zet `docker-compose.yml` in een map en draai
`docker compose up -d`.

### 3. Log in op Marktplaats

Bij **Instellingen** log je in met je Marktplaats-e-mailadres en wachtwoord. Heb
je sms-verificatie aan staan, dan vraagt de pagina daarna om de code. De sessie
die eruit komt blijft maanden geldig.

Vink *Wachtwoord versleuteld bewaren* aan als de tool zelf opnieuw moet inloggen
wanneer de sessie verloopt. Vraagt Marktplaats dan om een sms-code, dan moet je
toch even zelf inloggen. De sms-code wordt nooit bewaard.

Lukt inloggen niet, dan kun je onder "Cookies uit je browser plakken" de cookies
`MpSecurity` en `MpSslSecurity` van marktplaats.nl invullen.

### 4. Eerst proef, dan echt

De container start in **proefmodus**: Herplaatsen en Verwijderen tonen in de log
wat ze zouden doen en veranderen niets. Ziet dat er goed uit, zet dan
*Proefmodus* uit bij Instellingen. Herplaatst wordt daarna alleen wat je
aanklikt, tenzij je automatisch herplaatsen aanzet.

## Wat er op schijf staat

Sessie en wachtwoord staan samen versleuteld in `config/state.json`. De sleutel
staat bewust ergens anders: in de map `keys`, of in `SECRET_KEY` als je die zelf
zet. Een kopie van de map `config` alleen (een backup, een cloud-sync) is dus
niet genoeg om de login te lezen; neem `keys` daarom niet in dezelfde backup
mee. Wie volledige toegang tot de machine heeft, kan de login wel lezen. Raakt
de sleutel kwijt, dan log je opnieuw in.

## Wat het doet

- **Advertenties**: je live advertenties, met per stuk *Backup*, *Herplaatsen*,
  *Herplaatsen + aanpassen*, *Uitsluiten* en *Verwijderen*.
- **Backups**: wat er op schijf staat. Downloaden als zip, of als nieuwe
  advertentie plaatsen.
- **Geschiedenis** en **Log**: wat er gebeurd is.
- **Instellingen**: login, proefmodus, automatisch backuppen en herplaatsen,
  Telegram.

### Herplaatsen

1. maakt een verse backup,
2. uploadt de foto's en plaatst een nieuwe advertentie met dezelfde inhoud,
3. verwijdert de oude, pas als de nieuwe bestaat.

Mislukt stap 1 of 2, dan blijft de oude advertentie staan. De nieuwe advertentie
begint zonder biedingen, bewaarders en weergaven. Er wordt alleen gratis
geplaatst; waar plaatsen geld kost, weigert de tool.

### Herplaatsen + aanpassen

Hetzelfde als *Herplaatsen*, maar met een nieuwe prijs. Typ een bedrag of kies
−5%, −10% of −20%. Bij advertenties met bieden kun je ook *bieden vanaf*
aanpassen; laat je dat leeg, dan blijft het zoals het was, maar nooit hoger dan
de nieuwe prijs. Alleen voor advertenties met een vraagprijs (niet bij gratis,
"bieden" zonder prijs of "zie omschrijving"). De oude prijs en de nieuwe staan
in de geschiedenis.

### Wachtrij

Alles wat iets verandert op Marktplaats gaat door een wachtrij, één tegelijk.
Na een klik op *Herplaatsen*, *Plaatsen* of *Verwijderen* wacht de opdracht
eerst een willekeurige 30–90 seconden (instelbaar); in die tijd kun je hem nog
annuleren. Zo komen klikken nooit als één stoot bij Marktplaats aan.

### Proefmodus

Staat standaard aan. Herplaatsen en verwijderen worden dan alleen voorbereid en
in de log getoond; er gaat niets naar Marktplaats. Backups werken altijd.

### Automatisch backuppen

Standaard aan: elk uur krijgt elke advertentie die nog geen backup heeft er een.
Optioneel ruimt de tool backups op van advertenties die niet meer online staan,
na een instelbaar aantal dagen (standaard uit). Van advertenties die nog online
staan wordt nooit een backup opgeruimd.

### Automatisch herplaatsen

Standaard uit. Aan: de tool herplaatst zelf, de oudste eerst, advertenties die
minstens 14 dagen online staan, hooguit 3 per dag, tussen 09:00 en 22:00, met
2–10 minuten ertussen. Alles is instelbaar. Overgeslagen worden advertenties
die je hebt *uitgesloten*, die gereserveerd zijn of waar een bod op staat. In
proefmodus zie je in de log wat er herplaatst zou worden.

Begin klein: Marktplaats kan veel herplaatsen als misbruik zien.

### Telegram

Vul bij Instellingen een bot-token (van @BotFather) en je chat-id in en kies
wat je wilt horen: alleen mislukkingen, of ook elke herplaatsing. Een verlopen
login wordt ook gemeld. Het token wordt versleuteld bewaard.

## Backups

Eén map per advertentie in `backups/`:

```
backups/m1234567890_Houten-eettafel/
    ad.json      de advertentie: leesbaar bovenaan, daaronder de ruwe
                 Marktplaats-gegevens waarmee opnieuw geplaatst wordt
    meta.json    overzichtsgegevens (titel, prijs, foto's, datum)
    photos/      001.jpg, 002.jpg, ... de originele foto's, op volgorde
```

## Instellingen in docker-compose.yml

| Variabele | Betekenis |
|---|---|
| `WEB_USER` / `WEB_PASSWORD` | Login voor de pagina zelf. Geen wachtwoord = geen login. Voorbeeldwaarden als `password` en `CHANGE_ME` worden geweigerd. |
| `PUID` / `PGID` | De gebruiker waaronder de container draait. Weggelaten = root. |
| `SECRET_KEY` | Optioneel: eigen sleutel voor de versleutelde login. Weggelaten = een gegenereerd bestand in `/keys`. |
| `USER_AGENT` | Optioneel: de user agent waarmee de tool zich bij Marktplaats meldt. |
| `TZ` | Tijdzone, voor de actieve uren. |

Beginwaarden, alleen van kracht zolang je op de pagina nog nooit Instellingen
hebt opgeslagen: `DRY_RUN`, `AUTO_BACKUP`, `AUTO_REPOST`, `MAX_REPOSTS_PER_DAY`,
`MIN_ITEM_AGE_DAYS`, `MIN_DELAY_SECONDS`, `MAX_DELAY_SECONDS`, `ACTIVE_HOURS`,
`MANUAL_DELAY_MIN_SECONDS`, `MANUAL_DELAY_MAX_SECONDS`, `BACKUP_RETENTION_DAYS`,
`NOTIFY_ON`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

Zet poort 8096 niet open naar internet.

## Zelf bouwen

```bash
docker compose -f compose.local.yml up -d --build
```

Maak daarvoor eerst een `compose.local.yml` met `build: .` in plaats van
`image:` en je eigen mappen. Bij elke push naar `main` bouwt
`.github/workflows/build.yml` de image voor amd64 en arm64 en zet die op
`ghcr.io/<eigenaar>/mpmanager:latest`.

## Opbouw

| Bestand | Rol |
|---|---|
| `app/mp_api.py` | Alles richting Marktplaats, en de versleutelde login |
| `app/worker.py` | Backup, plaatsen, herplaatsen; de wachtrij en de planner |
| `app/store.py` | Backups, geschiedenis, instellingen en log op schijf |
| `app/notify.py` | Telegram |
| `app/app.py` | Webserver, eigen loginpagina, API |
| `app/entrypoint.py` | Start; wisselt naar PUID/PGID |
| `app/templates/` | De pagina's |

## Disclaimer

Marktplaats staat geautomatiseerd gebruik niet zonder meer toe, en massaal
herplaatsen kan je account kosten. Deze tool automatiseert wat je ook met de
hand kunt doen; gebruik is op eigen risico.
