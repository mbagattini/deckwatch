#!/usr/bin/env bash
# DeckWatch — installazione / aggiornamento / disinstallazione per Steam Deck.
#
#   Installa o aggiorna:  curl -fsSL <REPO_RAW>/install.sh | bash
#   Da una copia locale:  bash install.sh
#   Disinstalla:          bash install.sh --uninstall   (aggiungi --purge per cancellare dati e config)
#
# Tutto viene installato nella home dell'utente (sopravvive agli aggiornamenti di SteamOS),
# senza sudo.
set -euo pipefail

REPO_RAW="${DECKWATCH_REPO_RAW:-https://raw.githubusercontent.com/mbagattini/deckwatch/main}"
APP=deckwatch
INSTALL_DIR="$HOME/.local/share/$APP"
BIN_DIR="$HOME/.local/bin"
CFG_DIR="$HOME/.config/$APP"
STATE_DIR="$HOME/.local/state/$APP"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT="$UNIT_DIR/$APP.service"

say()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERRORE:\033[0m %s\n' "$*" >&2; exit 1; }

# input interattivo anche con "curl | bash"
TTY=/dev/tty
ask() { # ask "domanda" "default" -> stampa risposta
  local q="$1" def="${2:-}" ans=""
  if (exec <"$TTY") 2>/dev/null; then
    if [[ -n $def ]]; then printf '%s [%s]: ' "$q" "$def" >"$TTY"; else printf '%s: ' "$q" >"$TTY"; fi
    IFS= read -r ans <"$TTY" || true
  fi
  printf '%s' "${ans:-$def}"
}

uninstall() {
  say "Disinstallo DeckWatch"
  systemctl --user disable --now "$APP.service" 2>/dev/null || true
  rm -f "$UNIT" "$BIN_DIR/$APP"
  rm -rf "$INSTALL_DIR"
  systemctl --user daemon-reload 2>/dev/null || true
  if [[ ${1:-} == "--purge" ]]; then
    rm -rf "$CFG_DIR" "$STATE_DIR"
    say "Rimossi anche configurazione e dati."
  else
    say "Configurazione ($CFG_DIR) e dati ($STATE_DIR) conservati. Usa --purge per rimuoverli."
  fi
  exit 0
}

[[ ${1:-} == "--uninstall" ]] && uninstall "${2:-}"

# --- requisiti --------------------------------------------------------------
command -v python3 >/dev/null || die "python3 non trovato"
python3 - <<'EOF' || die "Serve Python 3.11 o superiore"
import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)
EOF

# --- file sorgente: copia locale o download -----------------------------------
SRC_DIR=""
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n $SELF_DIR && -f $SELF_DIR/deckwatch.py ]]; then
  SRC_DIR="$SELF_DIR"
  say "Installo dalla cartella $SRC_DIR"
else
  SRC_DIR="$(mktemp -d)"
  trap 'rm -rf "$SRC_DIR"' EXIT
  say "Scarico DeckWatch da $REPO_RAW"
  for f in deckwatch.py config.example.toml policy.example.toml VERSION install.sh; do
    curl -fsSL "$REPO_RAW/$f" -o "$SRC_DIR/$f" || die "download di $f fallito"
  done
fi
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "$SRC_DIR/deckwatch.py" \
  || die "deckwatch.py non valido"

# --- programma -------------------------------------------------------------
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$CFG_DIR" "$STATE_DIR" "$UNIT_DIR"
if [[ "$(cd "$SRC_DIR" && pwd -P)" != "$(cd "$INSTALL_DIR" && pwd -P)" ]]; then
  install -m 755 "$SRC_DIR/deckwatch.py" "$INSTALL_DIR/deckwatch.py"
  cp "$SRC_DIR/config.example.toml" "$SRC_DIR/policy.example.toml" "$INSTALL_DIR/"
  [[ -f $SRC_DIR/install.sh ]] && install -m 755 "$SRC_DIR/install.sh" "$INSTALL_DIR/install.sh"
fi
ln -sf "$INSTALL_DIR/deckwatch.py" "$BIN_DIR/$APP"
say "Programma installato in $INSTALL_DIR (comando: $APP)"

# --- regole ----------------------------------------------------------------
if [[ ! -f $CFG_DIR/policy.toml ]]; then
  cp "$SRC_DIR/policy.example.toml" "$CFG_DIR/policy.toml"
  say "Creato $CFG_DIR/policy.toml con limiti di esempio: modificalo con le regole concordate."
fi

# --- configurazione (solo alla prima installazione) ------------------------
FIRST_CONFIG=0
if [[ ! -f $CFG_DIR/config.toml ]]; then
  FIRST_CONFIG=1
  say "Configurazione iniziale (premi Invio per saltare una voce, potrai cambiarla dopo)"
  TG_TOKEN="$(ask 'Token del bot Telegram (da @BotFather)')"
  EM_USER="$(ask 'Gmail mittente per il report al figlio (es. deckwatch.famiglia@gmail.com)')"
  EM_PASS=""
  [[ -n $EM_USER ]] && EM_PASS="$(ask 'Password per le app di quel Gmail (16 lettere)')"
  EM_KID="$(ask 'Email di tuo figlio (per il suo report)')"
  REPORT_TIME="$(ask 'Ora del report giornaliero' '21:30')"
  TG_TOKEN="$TG_TOKEN" EM_USER="$EM_USER" EM_PASS="$EM_PASS" EM_KID="$EM_KID" \
  REPORT_TIME="$REPORT_TIME" REPO_RAW="$REPO_RAW" \
  python3 - "$SRC_DIR/config.example.toml" "$CFG_DIR/config.toml" <<'EOF'
import json, os, re, sys
src, dst = sys.argv[1], sys.argv[2]
t = open(src, encoding="utf-8").read()
q = lambda s: json.dumps(s, ensure_ascii=False)   # stringa TOML valida
def setkey(text, section, key, value):
    pat = re.compile(r"(\[%s\][^\[]*?^%s\s*=\s*)[^\n#]*" % (re.escape(section), key), re.M | re.S)
    return pat.sub(lambda m: m.group(1) + value + ("   " if "#" in m.group(0) else ""), text, count=1)
e = os.environ
t = re.sub(r'^report_time = .*$', 'report_time = ' + q(e["REPORT_TIME"]), t, count=1, flags=re.M)
t = re.sub(r'^update_url = .*$', 'update_url = ' + q(e["REPO_RAW"]), t, count=1, flags=re.M)
t = setkey(t, "telegram", "token", q(e["TG_TOKEN"]))
t = setkey(t, "email", "username", q(e["EM_USER"]))
t = setkey(t, "email", "password", q(e["EM_PASS"]))
if e["EM_KID"]:
    t = setkey(t, "email", "kid", q([e["EM_KID"]]))
fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(t)
EOF
  say "Creato $CFG_DIR/config.toml"
fi
chmod 600 "$CFG_DIR/config.toml"

# --- servizio systemd utente -------------------------------------------------
cat >"$UNIT" <<EOF
[Unit]
Description=DeckWatch - monitor del tempo di gioco
After=graphical-session.target

[Service]
ExecStart=/usr/bin/env python3 $INSTALL_DIR/deckwatch.py run
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

# --- collegamento dei genitori al bot Telegram ---------------------------------
if [[ $FIRST_CONFIG == 1 ]] && grep -qE '^token = "[^"]+' "$CFG_DIR/config.toml"; then
  systemctl --user stop "$APP.service" 2>/dev/null || true
  say "Ora ENTRAMBI i genitori: aprite il bot su Telegram e premete 'Avvia' (o scrivete /start)."
  ask "Quando l'avete fatto premi Invio" >/dev/null
  python3 "$INSTALL_DIR/deckwatch.py" telegram-setup --save || \
    warn "Nessun genitore collegato: ripeti più tardi con 'deckwatch telegram-setup --save'"
fi

systemctl --user enable "$APP.service" >/dev/null 2>&1
systemctl --user restart "$APP.service"
sleep 2
if systemctl --user is-active --quiet "$APP.service"; then
  say "DeckWatch $(cat "$SRC_DIR/VERSION" 2>/dev/null) è in esecuzione."
else
  warn "Il servizio non risulta attivo: controlla con  journalctl --user -u $APP -n 50"
fi

cat <<EOF

Fatto. Comandi utili (Konsole):
  deckwatch status              tempo di oggi
  deckwatch check               verifica le regole ($CFG_DIR/policy.toml)
  deckwatch games               elenco giochi Steam con appid
  deckwatch fg --watch          mostra l'app in primo piano (per scrivere le regole)
  deckwatch test                messaggio di prova su Telegram/email
  journalctl --user -u $APP -f  log in tempo reale
EOF
[[ ":$PATH:" == *":$BIN_DIR:"* ]] || warn "$BIN_DIR non è nel PATH: usa $BIN_DIR/deckwatch"
