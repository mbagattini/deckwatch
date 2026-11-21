# DeckWatch

Monitor del tempo di gioco per **Steam Deck** (SteamOS, Modalità Desktop e Modalità Gioco).

- Misura il tempo **per app/gioco**: conta solo l'app in primo piano, a Deck accesa
  e con input recente (tastiera, mouse, touch, controlli).
- Limiti **soft** (solo avviso) e **hard** (tempo di grazia, poi chiusura del gioco),
  per singola app e totali, diversi per giorno della settimana.
- **Report giornaliero** ai genitori su Telegram e al figlio via email.
- **Comandi remoti** dal bot Telegram: blocco (kill-switch), sblocco, minuti extra,
  stato, spegnimento.
- Solo Python standard, installato nella home: niente `sudo` e sopravvive agli
  aggiornamenti di SteamOS. Si aggiorna da solo.

## Come funziona il bot Telegram

Il bot non va ospitato: Telegram fa da casella postale. Lo script sulla Deck
controlla ogni pochi secondi se ci sono messaggi nuovi, esegue i comandi e risponde.
A Deck spenta i comandi restano in attesa (fino a 24 ore) e vengono eseguiti alla
riaccensione. Un `/spegni` arrivato con più di 10 minuti di ritardo viene ignorato.

## Preparazione (una volta sola, dal telefono o dal PC)

1. **Bot Telegram:** su Telegram apri `@BotFather` e scrivi `/newbot`. Scegli un
   nome, per esempio "DeckWatch Famiglia", e copia il **token** che ti dà.
2. **Brevo per il report email al figlio** (facoltativo): crea un account gratuito su
   brevo.com. In *Mittenti* aggiungi e conferma l'indirizzo da usare come mittente
   (anche il tuo Gmail: Brevo non vi accede). In *Impostazioni → SMTP & API → SMTP*
   crea una **chiave SMTP** e annota il **Login** mostrato lì. La chiave permette solo
   di inviare email: se finisse in mani sbagliate non dà accesso a nessuna casella, e
   si revoca con un clic. Le prime email potrebbero finire nello spam: segnale come
   "non spam".

## Installazione sulla Deck

1. Passa alla **Modalità Desktop** e apri **Konsole**.
2. Esegui:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/mbagattini/deckwatch/main/install.sh | bash
   ```
3. Rispondi alle domande: token del bot, dati Brevo, email del figlio, ora del report.
4. Quando richiesto, **entrambi i genitori** aprono il bot su Telegram e premono
   **Avvia**. L'installer registra le vostre chat.
5. Prova l'invio dei messaggi: `deckwatch test`.

Il servizio parte da solo a ogni accensione, sia in Modalità Desktop sia in
Modalità Gioco.

**Aggiornamento:** è automatico, con un controllo al giorno. Per forzarlo:
`deckwatch update`, oppure rilancia la riga `curl ... | bash` (configurazione e
regole non vengono toccate).

**Disinstallazione:** `bash ~/.local/share/deckwatch/install.sh --uninstall`
(aggiungi `--purge` per cancellare anche regole, configurazione e dati).

## Regole: `~/.config/deckwatch/policy.toml`

Il file è commentato in italiano. Esempio:

```toml
[total]                 # tempo totale giornaliero
soft = 100              # avviso
hard = 120              # massimo: poi grazia e chiusura
weekend = { soft = 150, hard = 180 }

[apps.geometry_dash]
name = "Geometry Dash"
match = ["steam:322170"]
soft = 45
hard = 60
```

- I giochi Steam si identificano con `steam:<appid>`. `deckwatch games` elenca
  quelli installati.
- Le altre app (browser, giochi non Steam) si identificano con `class:<nome>`.
  `deckwatch fg --watch` mostra il nome dell'app in primo piano.
- Il file viene ricaricato da solo entro circa 15 secondi. Se contiene un errore,
  arriva un messaggio Telegram e resta in uso la versione precedente. Per
  verificarlo: `deckwatch check`.
- Per un giorno particolare si usa la sezione `[exceptions.AAAA-MM-GG]`; per le
  eccezioni al volo c'è `/extra` su Telegram.

### Cosa succede al limite

1. **soft:** notifica.
2. **Avvisi a −10, −5 e −1 minuti** dal limite hard.
3. **Limite hard:** popup "tempo finito, hai 5 minuti per salvare".
4. **Fine della grazia:** il gioco viene chiuso e i genitori ricevono un avviso.
5. **Se lo riapre:** avviso, poi chiusura dopo 20 secondi.
6. **Tempo totale finito:** si chiudono tutte le app, tranne quelle in
   `always_allowed` (Steam, impostazioni, file). In alternativa,
   `on_total_exceeded = "shutdown"` spegne la Deck.

## Comandi Telegram (solo per i genitori)

| Comando | Effetto |
|---|---|
| `/stato` | cosa sta usando ora e il tempo di oggi |
| `/oggi` | report di oggi fin qui |
| `/limiti` | limiti di oggi, con gli extra |
| `/stop` | blocca la Deck finché non arriva `/sblocca` (60 secondi per salvare, poi chiude tutto e mostra un popup) |
| `/stop 60` | blocca per 60 minuti |
| `/sblocca` | toglie il blocco |
| `/extra 30` | 30 minuti in più sul totale di oggi (anche negativi) |
| `/extra geometry_dash 15` | 15 minuti in più a un'app |
| `/spegni` | spegne la Deck tra 30 secondi |

Ogni comando viene notificato anche all'altro genitore.

## Comandi sulla Deck

```
deckwatch status          tempo di oggi
deckwatch check           verifica policy.toml e mostra i limiti della settimana
deckwatch games           giochi Steam installati con appid
deckwatch fg --watch      app in primo piano e inattività (per scrivere le regole)
deckwatch report --date 2026-11-19 [--send]
deckwatch test            messaggi di prova
deckwatch telegram-setup --save   aggiunge un genitore (servizio fermo)
journalctl --user -u deckwatch -f log
```

## Limiti noti

- **Modalità Gioco:** le notifiche desktop non sono visibili. Conteggio, limiti e
  chiusura funzionano, ma gli avvisi soft non si vedono. È uno sviluppo futuro
  (plugin Decky Loader).
- **Browser:** conta come un'unica app, senza dettaglio per sito (scelta voluta).
- **Nessuna protezione anti-manomissione:** chi conosce Linux può fermare il
  servizio. Il sistema si basa sulla fiducia, come concordato.
- **Inattività con gamepad:** in Modalità Desktop viene rilevata leggendo i
  dispositivi di input accessibili all'utente. Se non lo sono, conta l'inattività di
  tastiera, mouse e touch, e un gioco usato solo col gamepad potrebbe risultare
  "inattivo" dopo `idle_minutes`. Verifica con `deckwatch fg --watch` e, se serve,
  alza `idle_minutes`.

## Spostare il repository

Il repository deve essere **pubblico** perché installazione e aggiornamenti
scarichino i file. Quando lo sposti:

1. Cambia `REPO_RAW` in `install.sh` e l'URL qui sopra.
2. Sulla Deck, cambia `update_url` in `~/.config/deckwatch/config.toml`.

## Sviluppo

```bash
python3 -m unittest discover -s tests
```
