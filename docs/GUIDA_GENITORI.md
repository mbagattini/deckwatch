# DeckWatch: guida per i genitori

DeckWatch è un programma che gira sulla Steam Deck. Misura quanto tempo viene
usata, per ogni gioco o app, e lo confronta con i tempi che avete concordato con
vostro figlio. A voi manda i report e gli avvisi su Telegram, a lui un riepilogo
per email.

Non registra cosa scrive, non fa foto dello schermo e non tiene la cronologia dei
siti visitati.

---

## Il tempo che conta

- Conta solo il tempo in cui un gioco o un'app è **davanti**, sullo schermo, e lui lo
  sta **usando**.
- Se non tocca nulla per **2 minuti** (né tasti, né schermo, né controlli), il
  conteggio si ferma. Riparte da solo appena tocca di nuovo qualcosa.
- Quando la Deck è spenta o in sospensione non conta niente.

**Cosa non conta come svago:**
- **Crescita.** Scuola, studio, programmazione: comprende siti e app scelti da voi.
  - **Siti:** il tempo su Firefox non conta, purché **tutte** le schede aperte siano su
    siti della lista. All'inizio la lista contiene Nuvola, Wikipedia, Classroom, Docs,
    Drive e Meet; potete aggiungere siti che gli concedete ma di cui volete sapere
    l'uso, per esempio chess.com o claude.ai. Basta una scheda di un altro sito
    aperta, anche in secondo piano (YouTube, un gioco online…), perché tutto il tempo
    conti come svago. Lo stesso vale per una finestra anonima o per un altro browser.
  - **App:** per esempio Scratch o un programma per imparare a programmare.
  - **Nel report** questo tempo compare come "🌱 Crescita" e non viene sommato allo
    svago. Avete il dettaglio sito per sito e app per app. La navigazione normale
    resta invece un unico totale "Browser", senza l'elenco dei siti.
- **Modalità insieme.** Quando giocate con lui, attivatela da Telegram e il tempo non
  conta.

## I tempi concordati

**Tipi di giorno:**
- **feriale**: da lunedì a venerdì;
- **festivo**: sabato e domenica.

Con un comando potete far valere oggi come festivo, per esempio in vacanza, o come
feriale.

**Cosa si può limitare:**
- il **tempo totale** della giornata, per i giorni feriali e per i festivi;
- **singoli giochi**, se volete. Per esempio Geometry Dash al massimo 1 ora, mentre
  Minecraft può non avere un limite suo e contare solo nel totale.

Ogni limite ha due valori:

| | Cosa succede |
|---|---|
| **Tempo concordato** | Sulla Deck compare "Il tempo concordato per … è terminato. Chiudi appena possibile.", senza suono per non distrarlo durante il gioco. |
| **Tempo massimo** (di solito il concordato + **5 minuti**) | Compare una finestra con un suono di avviso, che interrompe il gioco. Oggi, in prova, il gioco non viene chiuso; con le regole attive si chiude. |

## Prova e regole attive

DeckWatch ha due modalità:

- **Prova (monitor)**, attiva adesso. Il programma misura, avvisa e manda i report,
  ma non chiude mai i giochi per i limiti. Nel vostro report trovate scritto cosa
  sarebbe successo, per esempio "Geometry Dash sarebbe stato chiuso alle 17:05". È
  un modo per abituarsi insieme alle regole prima di renderle effettive.
- **Regole attive (enforce).** Al tempo massimo il gioco si chiude. Se lo riapre,
  compare un avviso e dopo **20 secondi** si richiude. A tempo totale finito restano
  usabili solo le app e i siti di crescita e le app di sistema.

La **pausa** (vedi sotto) funziona in entrambe le modalità.

## I report

- **Report giornaliero:** arriva la mattina dopo, alla prima accensione della Deck,
  oppure a mezzanotte se è accesa. Contiene la giornata intera.
- **Riepilogo settimanale:** la settimana va dal sabato al venerdì, e il riepilogo
  arriva **dal sabato**.
- **Vostro figlio** riceve per email la sua versione, senza la parte "eventi".

Nella parte alta di ogni report trovate il riepilogo:
- ✅ dentro il tempo concordato;
- 🟡 oltre il concordato, ma dentro il massimo;
- ❌ oltre il massimo.

Sotto ci sono i dettagli gioco per gioco. Nella vostra versione ci sono anche gli
**eventi** (avvisi, chiusure simulate, comandi mandati) e gli eventuali segnali di
manomissione.

Durante il giorno potete chiedere `/report_oggi` o `/report_settimana`.

## I comandi su Telegram

Non serve ricordarli: scrivete `/` nella chat del bot, oppure toccate **Menu**, e
compare l'elenco. Se toccate un comando che richiede dati, il bot vi risponde con un
esempio. `/info_comandi` mostra tutto.

**Sapere cosa succede**
- `/info_stato`: cosa sta usando adesso e quanto ha giocato oggi.
- `/report_oggi`, `/report_settimana`: i report fino a questo momento.
- `/info_limiti`: tutti i limiti in vigore, e se oggi è feriale o festivo.

**Intervenire oggi**
- `/timer_pausa`: mette in pausa lo svago. Lui ha **60 secondi** per salvare, poi i
  giochi si chiudono. App e siti di crescita restano utilizzabili. `/timer_pausa 60`
  vale per 60 minuti; `/timer_riprendi` toglie la pausa.
- `/timer_concedi_extra 30`: mezz'ora in più solo per oggi. Funziona anche per un
  singolo gioco: `/timer_concedi_extra geometry dash 15`.
- `/timer_considera_oggi_festivo` (o `_feriale`): cambia il tipo di giorno di oggi.
- `/timer_spegni_deck`: spegne la Deck dopo 30 secondi. In prova mostra solo
  l'avviso.

**Giocare insieme**
- `/insieme_inizio`: da questo momento il tempo non conta.
  - **Promemoria:** dopo **90 minuti** arriva un promemoria a voi e a lui, poi a voi
    ogni **30 minuti**, finché non mandate `/insieme_fine`.
  - **Promemoria anticipato:** con `/insieme_inizio 60` il primo arriva dopo 60.
  - **Chiusura:** a mezzanotte la modalità si chiude comunque da sola.
  - **Pausa:** se c'era una pausa attiva, resta sospesa finché giocate insieme.

**Cambiare le regole** (valgono subito)
- `/limite_imposta geometry dash 60`: concordato 1 ora, massimo 1 ora e 5 minuti, per
  tutti i giorni.
- `/limite_imposta geometry dash festivo 90 100`: solo per i giorni festivi,
  concordato e massimo scelti da voi.
- `/limite_imposta totale feriale 120`: il tempo totale nei giorni feriali.
- `/limite_rimuovi minecraft`: toglie il limite di un gioco, che continua a contare
  nel totale.
- `/config_aggiungi_sito_crescita chess.com` e `/config_rimuovi_sito_crescita`:
  gestiscono i siti di crescita. Si può incollare anche un link intero.
- `/config_aggiungi_app_crescita scratch` e `/config_rimuovi_app_crescita`:
  gestiscono le app di crescita. L'app deve essere stata aperta almeno una volta
  sulla Deck.
- `/info_crescita`: mostra siti e app di crescita, con il tempo di oggi.
- `/config_nome_app`: dà un nome leggibile a un'app. Il bot ve lo propone da solo
  quando vede un'app nuova.

Ogni volta che uno di voi manda un comando che cambia qualcosa, l'altro genitore
riceve un messaggio.

**Se la Deck è spenta o senza connessione**, i comandi aspettano che si riaccenda,
ma non tutti restano validi:
- **Richieste di informazioni** (`info_…`, `report_…`): se non hanno risposta entro
  1 minuto vengono scartate. Ripetetele quando la Deck è accesa.
- **Azioni su "adesso"** (`timer_…`, `insieme_…`): se arrivano con più di 10 minuti di
  ritardo vengono ignorate, e il bot ve lo dice.
- **Regole e configurazione** (`limite_…`, `config_…`): vengono applicate appena la
  Deck si riaccende.

## Messaggi che possono arrivarvi

- **🆕 Nuova app:** un'app mai vista prima è stata usata per più di **5 minuti**. Il
  messaggio contiene i comandi pronti per darle un nome o un limite.
- **⛔ Gioco chiuso** (solo con le regole attive).
- **⚠️ Segnali di manomissione:**
  - DeckWatch è rimasto fermo per più di **3 minuti** con la Deck accesa;
  - DeckWatch è partito tardi dopo l'accensione, o si è fermato prima dello
    spegnimento;
  - l'orologio della Deck è sfasato di più di **5 minuti**.

  Nel report trovate anche le modifiche fatte a mano ai file delle regole e il tempo
  di gioco senza connessione internet.
- **🔄 Aggiornamento:** DeckWatch si aggiorna da solo, al massimo una volta al giorno,
  aspettando un momento in cui lui non sta giocando. Potete anche lanciarlo con
  `/config_aggiorna`.

## Tempi in breve

| | |
|---|---|
| Inattività dopo cui il tempo smette di contare | 2 minuti |
| Tempo massimo, se indicate solo il concordato | concordato + 5 minuti |
| Tempo per salvare quando parte una pausa | 60 secondi |
| Gioco riaperto quando non è permesso (regole attive) | chiuso dopo 20 secondi |
| Avviso "nuova app" | dopo 5 minuti di uso |
| Modalità insieme | promemoria a 90 minuti, poi ogni 30; chiusura a mezzanotte |
| Report giornaliero | il giorno dopo, alla prima accensione |
| Riepilogo settimanale | dal sabato (settimana sabato → venerdì) |

## Cosa DeckWatch non fa

- **Non è a prova di esperto.** Chi conosce Linux può fermarlo; è pensato su un
  accordo di fiducia. Se però qualcuno lo ferma, ve ne accorgete dagli avvisi.
- **Non salva i siti visitati.** Guarda solo se le schede di Firefox sono tutte di
  crescita, e poi dimentica gli indirizzi. Registra il tempo solo per i siti della
  lista di crescita.
- **Non vede le altre app dall'interno.** Non sa cosa succede dentro un gioco o
  un'app, né cosa viene scritto.
