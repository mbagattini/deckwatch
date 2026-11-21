# DeckWatch

Monitor del tempo di gioco per **Steam Deck** (SteamOS, soprattutto Modalità Desktop,
ma funziona anche in Modalità Gioco).

Guide in linguaggio semplice:
- [Guida per i genitori](docs/GUIDA_GENITORI.md)
- [Guida per il ragazzo](docs/GUIDA_RAGAZZO.md)

Questo file contiene i dettagli tecnici.

## Cosa fa

- **Conteggio del tempo per app:** conta solo l'app in primo piano, con la Deck non
  sospesa e con input negli ultimi 2 minuti (tastiera, mouse, touch, controlli).
- **Limiti per tipo di giorno:** feriale (lun–ven) e festivo (sab–dom). Il totale
  della giornata ha sempre un limite; le singole app solo se lo imposti. Ogni limite
  ha due valori:
  - *concordato*: arriva un avviso "chiudi appena possibile";
  - *massimo*: in modalità enforce l'app si chiude; in modalità monitor arriva solo un
    avviso.
- **Crescita** (scuola, studio, programmazione…): tempo che non conta come svago.
  - **Siti:** valgono solo quando Firefox è in primo piano e **tutte** le schede aperte
    sono su siti della lista.
  - **App:** per esempio Scratch o un editor di codice.

  Nel report il tempo di crescita ha il dettaglio per sito e per app; la navigazione
  normale resta un unico totale "Browser".
- **Modalità insieme:** il tempo passato a giocare con i genitori non conta.
- **Report:** giornaliero e settimanale (sabato→venerdì), ai genitori su Telegram e al
  figlio via email (Brevo).
- **Bot Telegram:** comandi per i genitori, con il menu registrato automaticamente.
- **Anti-manomissione di base:** avvisa se il servizio viene fermato, se la Deck viene
  usata senza DeckWatch o se l'orologio viene spostato; nel report segnala le
  modifiche fatte a mano ai file e l'uso senza connessione.
- **Aggiornamento automatico:** una volta al giorno, in un momento in cui non sta
  giocando. Se una versione nuova va in crash torna alla precedente.
- **Requisiti:** solo Python standard, installato nella home; non serve `sudo`.

## Installazione

Sulla Deck, in Modalità Desktop, apri Konsole:
```bash
curl -fsSL https://raw.githubusercontent.com/mbagattini/deckwatch/main/install.sh | bash
```
- **Domande dell'installer:** compaiono solo alla prima installazione. Ti chiede il
  token del bot Telegram, poi i dati Brevo (login SMTP, chiave SMTP, mittente
  verificato) e l'email del figlio.
- **Genitori:** ognuno apre il bot su Telegram e preme **Avvia**; l'installer
  registra le chat.
- **Prove:** `deckwatch test` verifica Telegram e l'email; `deckwatch test-notifiche`
  verifica cosa si vede sulla Deck.
- **Aggiornare:** rilancia la stessa riga, oppure usa `/configura_aggiorna` su
  Telegram. Configurazione e limiti restano.
- **Disinstallare:** `bash ~/.local/share/deckwatch/install.sh --uninstall`
  (aggiungi `--purge` per cancellare anche configurazione e dati).

## File sulla Deck

| File | Chi lo modifica | Contenuto |
|---|---|---|
| `~/.config/deckwatch/config.toml` | a mano, una volta | token Telegram, chat dei genitori, dati Brevo |
| `~/.config/deckwatch/policy.toml` | a mano | impostazioni tecniche: modalità, inattività, margini, app escluse |
| `~/.config/deckwatch/limiti.toml` | il bot (anche a mano) | limiti feriali/festivi, nomi delle app, siti e app di crescita |
| `~/.local/state/deckwatch/` | il programma | dati delle giornate, coda dei messaggi, stato dei comandi |

I file delle regole vengono ricaricati da soli entro circa 15 secondi. Se contengono
un errore, arriva un messaggio su Telegram e resta in uso la versione precedente.
Per verificarli: `deckwatch check`.

Chi aggiornava da una versione precedente non deve fare niente: i limiti scritti nel
vecchio `policy.toml` vengono spostati automaticamente in `limiti.toml`, e una copia
del vecchio file resta in `policy.toml.bak`.

## Comandi Telegram

Scrivi `/` o tocca il pulsante **Menu** per l'elenco. Se tocchi un comando che
richiede dati, il bot risponde con un esempio.

| Comando | Effetto |
|---|---|
| `/report_oggi`, `/report_settimana` | report di oggi e della settimana fin qui |
| `/info_stato` | cosa sta usando ora e il tempo di oggi |
| `/info_limiti` | limiti in vigore e tipo di giorno |
| `/info_app` | app viste negli ultimi 7 giorni, con identificativo |
| `/info_crescita` | siti e app di crescita, con il tempo di oggi |
| `/info_versione` | versione installata e ultimo controllo degli aggiornamenti |
| `/info_comandi` | elenco dei comandi con esempi |
| `/timer_pausa [minuti]`, `/timer_riprendi` | pausa dello svago (app e siti di crescita restano utilizzabili) |
| `/timer_concedi_extra [app] minuti` | minuti in più solo per oggi |
| `/timer_considera_oggi_festivo` / `_feriale` | cambia il tipo di giorno di oggi |
| `/timer_spegni_deck` | spegne la Deck (in monitor: avviso simulato) |
| `/insieme_inizio [minuti]`, `/insieme_fine` | modalità insieme (promemoria predefinito a 90 minuti) |
| `/limite_imposta <app\|totale> [feriale\|festivo] concordato [massimo]` | massimo predefinito: concordato + 5 |
| `/limite_rimuovi <app\|totale> [feriale\|festivo]` | toglie un limite |
| `/configura_nome_app <identificativo> <nome>` | dà un nome leggibile a un'app |
| `/configura_aggiungi_sito_crescita`, `/configura_rimuovi_sito_crescita` | siti di crescita |
| `/configura_aggiungi_app_crescita`, `/configura_rimuovi_app_crescita` | app di crescita (per nome o identificativo) |
| `/configura_aggiorna` | aggiorna subito |

Ogni modifica viene notificata anche all'altro genitore.

## Comandi sulla Deck

```
deckwatch status          tempo di oggi
deckwatch check           verifica policy.toml e limiti.toml
deckwatch games           giochi Steam installati con appid
deckwatch fg --watch      app in primo piano, inattività, stato "crescita"
deckwatch report --date 2026-11-19 [--send]
deckwatch test            messaggi di prova Telegram ed email
deckwatch test-notifiche  prova notifiche, popup e suono
deckwatch telegram-setup --save   aggiunge un genitore (a servizio fermo)
journalctl --user -u deckwatch -f log
```

## Note tecniche

- **Campionamento:** l'app in primo piano viene controllata ogni 10 secondi; l'input
  del gamepad ogni secondo.
- **Priorità:** il servizio gira con priorità minima per processore e disco.
- **Firefox:** il file di sessione (`recovery.jsonlz4`) viene aggiornato da Firefox
  ogni circa 15 secondi. Gli indirizzi vengono solo confrontati con la whitelist e
  non vengono salvati. Le finestre anonime non compaiono nella sessione: se esiste
  una finestra di Firefox che la sessione non conosce, il tempo conta come svago.
- **Modalità Gioco:** le notifiche di sistema non si vedono, il suono sì.
- **Repository:** deve essere pubblico perché installazione e aggiornamenti
  funzionino.

## Sviluppo

```bash
python3 -m unittest discover -s tests
```
