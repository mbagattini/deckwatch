# DeckWatch — memoria di progetto

Questo file serve a riprendere il lavoro in una nuova sessione: contesto, decisioni prese,
stato attuale e prossimi passi. Va aggiornato a ogni sessione di lavoro significativa.

## Contesto

- **Cos'è:** un monitor del tempo di gioco per la Steam Deck di un ragazzo di 11 anni,
  voluto dai genitori e concordato con lui (sistema basato sulla fiducia).
- **Uso:** soprattutto in Modalità Desktop (KDE/X11), a volte in Modalità Gioco.
- **Per chi:** i genitori sono due. Ricevono report e avvisi su Telegram e comandano il
  bot. Il ragazzo riceve un report via email (Brevo).
- **Come lavorare con l'utente:**
  - parla italiano e spesso lavora dal telefono (app Claude iOS);
  - preferisce discutere un punto alla volta e decidere tutto **prima** di scrivere
    codice;
  - quando è davanti alla Deck, le istruzioni vanno passo per passo, con i comandi
    pronti da copiare.
- **Privacy:** il repo è **pubblico**. Niente dati personali nel repo: email, chat id,
  nome del ragazzo. La descrizione del bot con il suo nickname sta solo in
  `config.toml` sulla Deck.

## Architettura

- **Codice:** un solo file, `deckwatch.py`, che usa solo la libreria standard di
  Python (≥ 3.11). Installato in `~/.local/share/deckwatch/`, avviato come servizio
  systemd utente tramite `launcher.py` (gestisce il ritorno alla versione precedente).
  Il servizio gira con `Nice=19` e `IOSchedulingClass=idle`.
- **File sulla Deck:**
  - `~/.config/deckwatch/config.toml`: token Telegram, chat dei genitori, `description`
    del bot, dati SMTP Brevo (`smtp-relay.brevo.com:587`, login SMTP, chiave SMTP,
    mittente verificato).
  - `~/.config/deckwatch/policy.toml`: impostazioni tecniche (modalità, inattività,
    margini, countdown, app escluse e consentite). Lo modificano solo i genitori, a
    mano.
  - `~/.config/deckwatch/limiti.toml`: limiti feriali/festivi, app con nome e
    `match`, `[crescita]` con siti e app. Lo scrive il bot; si può modificare anche
    a mano. Il vecchio `[scuola]` viene letto e riscritto come `[crescita]`.
  - `~/.local/state/deckwatch/`: `days/AAAA-MM-GG.json`, `overrides.json` (pausa,
    extra, tipo di giorno, modalità insieme, ultimo settimanale), `meta.json`,
    `heartbeat.json`, `update.json`, `outbox/`.
- **Aggiornamento:** controllo giornaliero di `VERSION` su `main` (raw GitHub). Per
  distribuire una modifica **bisogna aumentare `VERSION` e `__version__`**.
- **Test:** `python3 -m unittest discover -s tests`. Usano FakePlatform e FakeOutbox.
- **Esempi:** `policy.example.toml` deve essere identico a `render_policy()`; un test
  lo verifica.

## Decisioni prese (riferimento)

1. **Conteggio**
   - Solo l'app in primo piano, con la Deck non sospesa e con input negli ultimi
     **2 minuti**.
   - Campionamento ogni **10 secondi**; gamepad letto da evdev ogni secondo.
2. **Limiti**
   - Due tipi di giorno: **feriale** (lun–ven) e **festivo** (sab–dom). Nessuna
     festività automatica.
   - `/timer_considera_oggi_festivo` e `_feriale` valgono solo per oggi; nessuna
     programmazione anticipata.
   - C'è un totale della giornata e, facoltativi, limiti per singola app.
   - **Concordato** (soft): un solo avviso, notifica critica **senza suono**: "Il tempo
     concordato per X è terminato. Chiudi appena possibile."
   - **Massimo** (hard): predefinito concordato **+5 minuti**. Popup più suono di
     allarme.
     - in monitor: "…L'applicazione continua a funzionare, ma in questo momento si
       sarebbe chiusa automaticamente";
     - in enforce: chiusura immediata, senza tempo di grazia. Se riapre l'app: avviso
       e chiusura dopo 20 secondi.
   - Countdown predisposto (`countdown_soft` e `countdown_hard`), oggi vuoto.
   - Le modifiche ai limiti valgono subito.
3. **Modalità**
   - `monitor` è quella attuale: niente chiusure per i limiti, solo simulazione nel
     report.
   - `enforce` va attivata in futuro.
   - `/timer_concedi_extra` funziona anche in monitor.
   - `/timer_spegni_deck` in monitor mostra solo un avviso simulato.
4. **Pausa** (`/timer_pausa [min]`)
   - Funziona anche in monitor e mette in pausa lo **svago**: app e siti di crescita
     restano utilizzabili.
   - 60 secondi per salvare; l'inizio della pausa è salvato e sopravvive ai riavvii.
   - Il blocco totale è rimandato al punto 8.
5. **Crescita** (dalla v0.4.0, prima "scuola")
   - Tempo che non conta come svago. Comprende **siti** e **app**, per esempio Scratch
     o un editor di codice.
   - **Report:** dettaglio per sito (attribuito alla scheda attiva) e per app. La
     navigazione normale resta cumulativa ("Browser"), senza siti. Esempi di siti
     concessi ma da misurare: chess.com, claude.ai.
   - **Siti:** il tempo non conta come svago solo se Firefox è in primo piano e
     **tutte** le schede di tutte le finestre sono nella lista.
   - Le schede si leggono da `sessionstore-backups/recovery.jsonlz4`, con un
     decompressore LZ4 scritto a mano. Gli indirizzi non vengono salvati.
   - Le finestre di Firefox sullo schermo non conosciute dalla sessione (per esempio
     le anonime) fanno contare il tempo come svago. Nel dubbio si conta.
   - Le pagine interne di Firefox (`about:`) sono consentite.
   - **Lista iniziale:** `nuvola.madisoft.it`, `*.wikipedia.org`,
     `classroom.google.com`, `docs.google.com`, `drive.google.com`,
     `meet.google.com`, `accounts.google.com`.
   - **Comandi:** `/config_aggiungi_sito_crescita`, `/config_rimuovi_sito_crescita`,
     `/config_aggiungi_app_crescita` (per nome o identificativo),
     `/config_rimuovi_app_crescita`, `/info_crescita`.
6. **Modalità insieme**
   - `/insieme_inizio [min]`: il tempo va in "Insieme" e non conta.
   - Promemoria a 90 minuti (sulla Deck e su Telegram), poi ogni 30 solo su Telegram.
   - Si chiude con `/insieme_fine` o a mezzanotte. Sospende la pausa.
7. **App nuove**
   - Avviso immediato su Telegram dopo 5 minuti di uso, con i comandi pronti per
     nome e limite.
   - `/config_nome_app`; limiti da Telegram con ricerca per nome tra i giochi
     Steam installati.
8. **Report**
   - **Giornaliero:** a fine giornata, cioè alla prima accensione del giorno dopo o a
     mezzanotte.
   - **Settimanale:** sabato→venerdì, inviato dal sabato.
   - Riepilogo compatto in alto (effettivo / concordato, ✅🟡❌), poi il dettaglio.
     Solo nel report dei genitori ci sono gli eventi.
9. **Anti-manomissione**
   - Avviso subito per:
     - servizio fermo per più di 3 minuti con la Deck accesa (battito ogni minuto);
     - partenza tardiva o fermata prima dello spegnimento (dal registro di sistema);
     - orologio sfasato di più di 5 minuti (dall'intestazione Date di Telegram).
   - Nel report: modifiche fatte a mano ai file, tempo offline durante l'uso.
10. **Aggiornamenti**
    - Automatici: aspettano che lui non stia giocando. Da Telegram:
      `/config_aggiorna`.
    - Se la nuova versione va in crash 3 volte nei primi 10 minuti, il launcher torna
      alla precedente.
11. **Comandi Telegram**
    - Prefissi: `report_`, `info_`, `timer_`, `insieme_`, `limite_`, `config_`
      (nomi corti: su Telegram si leggono male se sono lunghi).
      Elenco completo in `COMMANDS` dentro `deckwatch.py`.
    - Il menu è registrato da solo, solo nelle chat dei genitori.
    - Ogni modifica viene notificata all'altro genitore.
    - **Comandi arrivati in ritardo** (Deck spenta o offline):
      - `info_*`, `report_*` e `/start`: scartati in silenzio dopo 1 minuto;
      - `timer_*` e `insieme_*`: scartati dopo 10 minuti, con una risposta
        "ignorato";
      - `limite_*` e `config_*`: applicati sempre.
    - **Regola di manutenzione:** quando si rinomina un comando non si tengono alias
      dei nomi vecchi; i **dati** delle versioni precedenti invece vanno sempre
      migrati.
12. **Email:** solo via Brevo, con una chiave SMTP di solo invio. Niente password per
    le app di Google.

## Stato (aggiornato al 21/11/2026, versione 0.4.1)

La 0.4.0 introduce la "crescita" (siti e app con dettaglio nel report) al posto della
"scuola". Non è ancora provata sulla Deck: da verificare con
`deckwatch debug-firefox` (deve indicare il sito della scheda attiva) e con
`/config_aggiungi_app_crescita`.

**Verificato sulla Deck reale**
- Installazione e aggiornamento automatico.
- Telegram ed email Brevo (`deckwatch test`).
- **Notifiche con un gioco a tutto schermo:**
  - la notifica normale non si vede;
  - la notifica critica si vede;
  - il popup ruba il controllo dello schermo e riduce il gioco;
  - il suono si sente.

  Da qui la scelta: notifica critica al tempo concordato, popup più suono al massimo.
- **Whitelist Firefox** (Flatpak, profilo in
  `~/.var/app/org.mozilla.firefox/.mozilla/firefox/`): sì con solo Wikipedia, no con
  YouTube (verificato con la 0.3.2, quando si chiamava ancora "scuola"). Subito dopo l'apertura di Firefox
  `recovery.jsonlz4` non esiste per un po' e il tempo conta come svago: è accettato.

**Non ancora verificato**
- Inattività con il gamepad (`deckwatch fg --watch` giocando solo con i controlli).
  Lui lo usa poco.
- Menu Telegram e `/info_limiti` dopo la migrazione dei limiti da v0.2: il vecchio
  `hard` è diventato il concordato, e il massimo è concordato + 5.
- Primo report giornaliero e settimanale nel nuovo formato.
- Whitelist in Modalità Gioco: probabilmente non funziona (manca `_NET_CLIENT_LIST`),
  quindi lì conta come svago.

## Da discutere / fare

- **Punto 8:** blocco totale e spegnimento quando si passerà a `enforce` (comportamento
  di `on_total_exceeded`, `/timer_spegni_deck` reale).
- **Prova del gamepad.**
- **Più avanti, se serve:**
  - protezione vera (servizio di sistema, password sudo tenuta dai genitori);
  - plugin Decky Loader per le notifiche in Modalità Gioco.

## Comandi utili sulla Deck

```
deckwatch update | check | status | fg --watch | games | test | test-notifiche | debug-firefox
systemctl --user restart deckwatch ; journalctl --user -u deckwatch -f
```
Sul layout italiano la tilde si fa con AltGr+ì; in alternativa si usa `$HOME`.
