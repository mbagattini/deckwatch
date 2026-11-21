import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import deckwatch as d  # noqa: E402

LIMITS = """
[totale]
feriale = { concordato = 120, massimo = 125 }
festivo = { concordato = 180, massimo = 185 }

[app.gd]
nome = "Geometry Dash"
match = ["steam:322170"]
feriale = { concordato = 60, massimo = 65 }
festivo = { concordato = 60, massimo = 65 }

[app.browser]
nome = "Browser"
match = ["class:firefox"]
"""

GD = d.Foreground("steam:322170", "Geometry Dash", steam_appid="322170")
FF = d.Foreground("class:firefox", "firefox", window=5)
MC = d.Foreground("class:minecraft", "Minecraft", window=6)
STEAM = d.Foreground("class:steam", "steam", window=7)
SHELL = d.Foreground("class:plasmashell", "plasmashell")


class FakePlatform:
    def __init__(self):
        self.fg = None
        self.idle = 0.0
        self.growth = None   # etichetta del sito attivo se Firefox è "crescita"
        self.notes, self.popups, self.kills, self.steam_kills = [], [], [], []
        self.alarms = 0
        self.games = {}
        self.powered_off = False
        self.library = {"322170": "Geometry Dash", "1145360": "Hades"}

    def foreground(self): return self.fg
    def idle_seconds(self): return self.idle
    def notify(self, t, b, urgent=False): self.notes.append((t, b, urgent))
    def popup(self, text): self.popups.append(text)
    def alarm(self): self.alarms += 1
    def kill(self, fg): self.kills.append(fg.key); self.fg = None
    def kill_steam(self, appid): self.steam_kills.append(appid); self.games.pop(appid, None)
    def running_steam_games(self): return dict(self.games)
    def steam_name(self, appid): return self.library.get(appid)
    def steam_find(self, t): return [(a, n) for a, n in self.library.items() if t in n.lower()]
    def firefox_growth(self, sites): return self.growth
    def poweroff(self): self.powered_off = True


class FakeOutbox:
    def __init__(self): self.parents, self.kid, self.direct = [], [], []
    def to_parents(self, text, subject="DeckWatch"): self.parents.append((subject, text))
    def to_kid(self, text, subject): self.kid.append((subject, text))
    def enqueue(self, ch, to, subj, body): self.direct.append((to, body))


class Base(unittest.TestCase):
    policy_text = 'mode = "monitor"'
    limits_text = LIMITS
    start = dt.datetime(2026, 11, 19, 15, 0, 0)  # giovedì (feriale)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.pf = FakePlatform()
        self.ob = FakeOutbox()
        self.cfg = d.Config(tg_token="x", tg_parents=[1, 2])
        self.store = d.DayStore(self.root / "days")
        self.saved = []
        self.overrides = d.JsonFile(self.root / "ov.json")
        self.meta = d.JsonFile(self.root / "meta.json")
        self.eng = self.make_engine()
        self.now = self.start
        self.eng.start(self.now)

    def make_engine(self):
        pol = d.Policy(d.load_toml_text(self.policy_text))
        lim = d.Limits(d.load_toml_text(self.limits_text), pol.default_hard_margin)
        return d.Engine(self.pf, self.store, self.ob, self.cfg, pol, lim, self.overrides,
                        self.meta, save_limits=self.saved.append)

    def tearDown(self):
        self.tmp.cleanup()

    def run_for(self, seconds, step=10):
        t = 0
        while t < seconds:
            self.now += dt.timedelta(seconds=step)
            self.eng.tick(self.now, step)
            t += step

    def cmd(self, text, who="Papà", chat=1):
        return self.eng.handle_command(chat, who, text, self.now.timestamp(), self.now)

    def titles(self):
        return [n[0] for n in self.pf.notes]


class CountingTests(Base):
    def test_counts_only_active_foreground(self):
        self.pf.fg = MC
        self.run_for(60)
        self.assertAlmostEqual(self.eng.state["total"], 60)
        self.pf.idle = 130  # oltre 2 minuti: inattivo
        self.run_for(60)
        self.assertAlmostEqual(self.eng.state["total"], 60)
        self.pf.idle = 0
        self.pf.fg = SHELL  # escluso
        self.run_for(60)
        self.pf.fg = None
        self.run_for(60)
        self.assertAlmostEqual(self.eng.state["total"], 60)

    def test_growth_firefox_not_counted_with_site_detail(self):
        self.pf.fg = FF
        self.pf.growth = "wikipedia.org"
        self.run_for(600)
        self.pf.growth = "chess.com"
        self.run_for(300)
        self.assertAlmostEqual(self.eng.state["total"], 0)
        self.assertAlmostEqual(self.eng.state["growth"], 900)
        self.pf.growth = None  # c'è una scheda non consentita
        self.run_for(60)
        self.assertAlmostEqual(self.eng.state["total"], 60)
        body = d.build_report(self.eng.state, self.eng.limits, "parents")[1]
        self.assertIn("🌱 Crescita: 15 min (non conteggiato)", body)
        self.assertIn("DETTAGLIO CRESCITA\n• wikipedia.org: 10 min\n• chess.com: 5 min", body)
        self.assertIn("• Browser: 1 min", body)   # navigazione normale: solo cumulativo

    def test_growth_app(self):
        scratch = d.Foreground("class:scratch-desktop", "Scratch 3.0", window=9)
        self.pf.fg = scratch
        self.run_for(60)
        self.assertIn("Fatto", self.cmd("/configura_aggiungi_app_crescita scratch"))
        self.run_for(600)
        self.assertAlmostEqual(self.eng.state["total"], 60)
        self.assertAlmostEqual(self.eng.state["growth_detail"]["Scratch 3.0"], 600)
        self.assertIn("Scratch 3.0 (class:scratch-desktop)", self.cmd("/info_crescita"))
        self.cmd("/timer_pausa")
        self.run_for(120)
        self.assertEqual(self.pf.kills, [])   # le app di crescita restano usabili in pausa
        self.cmd("/configura_rimuovi_app_crescita class:scratch-desktop")
        self.assertEqual(self.eng.limits.growth_apps, [])


class MonitorTests(Base):
    def test_soft_then_hard_simulated(self):
        self.pf.fg = GD
        self.run_for(60 * 60)
        self.assertIn("⏰ Tempo concordato terminato", self.titles())
        self.assertIn("Il tempo concordato per Geometry Dash è terminato. Chiudi appena possibile.",
                      [n[1] for n in self.pf.notes])
        self.assertEqual(self.pf.alarms, 0)  # niente suono al tempo concordato
        self.assertTrue([n for n in self.pf.notes if n[0] == "⏰ Tempo concordato terminato"][0][2])
        self.assertEqual(self.titles().count("⏰ Tempo concordato terminato"), 1)
        self.run_for(6 * 60)
        hard = [n for n in self.pf.notes if n[0] == "⛔ Tempo massimo raggiunto"]
        self.assertEqual(len(hard), 1)
        self.assertIn("si sarebbe chiusa automaticamente", hard[0][1])
        self.assertTrue(self.pf.popups)
        self.assertEqual(self.pf.alarms, 1)  # suono solo al massimo
        self.assertEqual(self.pf.kills, [])
        sims = [e for e in self.eng.state["events"] if e[2] == "sim"]
        self.assertEqual(len(sims), 1)

    def test_no_countdown_by_default(self):
        self.pf.fg = GD
        self.run_for(59 * 60)
        self.assertNotIn("⏳ Countdown", self.titles())

    def test_pause_works_in_monitor_and_growth_survives(self):
        self.pf.fg = MC
        self.cmd("/timer_pausa")
        self.run_for(30)
        self.assertEqual(self.pf.kills, [])  # 60 s per salvare
        self.assertIn("⏸ Deck in pausa", self.titles())
        self.run_for(40)
        self.assertEqual(self.pf.kills, ["class:minecraft"])
        self.pf.fg = FF
        self.pf.growth = "wikipedia.org"
        self.run_for(60)
        self.assertEqual(self.pf.kills, ["class:minecraft"])  # Firefox su siti di crescita resta
        self.pf.growth = None
        self.run_for(10)
        self.assertTrue(any("non è disponibile ora" in t for t in self.titles()))
        self.run_for(30)
        self.assertIn("class:firefox", self.pf.kills)
        self.cmd("/timer_riprendi")
        self.pf.fg = MC
        self.run_for(60)
        self.assertEqual(self.pf.kills.count("class:minecraft"), 1)

    def test_pause_survives_restart_without_new_grace(self):
        self.pf.fg = MC
        self.cmd("/timer_pausa")
        self.run_for(30)
        eng2 = self.make_engine()
        eng2.start(self.now)
        self.eng = eng2
        self.run_for(40)
        self.assertEqual(self.pf.kills, ["class:minecraft"])

    def test_spegni_simulated(self):
        r = self.cmd("/timer_spegni_deck")
        self.assertIn("NON è stata spenta", r)
        self.assertFalse(self.pf.powered_off)
        self.assertIn("⏻ Spegnimento (simulazione)", self.titles())


class EnforceTests(Base):
    policy_text = 'mode = "enforce"'

    def test_hard_closes_and_relaunch(self):
        self.pf.fg = GD
        self.run_for(65 * 60 + 10)
        self.assertEqual(self.pf.kills, ["steam:322170"])
        self.assertTrue(any("Chiuso" in p[1] for p in self.ob.parents))
        self.pf.fg = GD
        self.run_for(10)
        self.assertEqual(len(self.pf.kills), 1)
        self.run_for(20)
        self.assertEqual(len(self.pf.kills), 2)
        self.pf.fg = MC  # le altre app restano usabili
        self.run_for(60)
        self.assertEqual(len(self.pf.kills), 2)

    def test_total_blocks_except_allowed_and_growth(self):
        self.pf.fg = MC
        self.run_for(125 * 60 + 10)
        self.assertIn("class:minecraft", self.pf.kills)
        n = len(self.pf.kills)
        self.pf.fg = STEAM
        self.run_for(60)
        self.pf.fg = FF
        self.pf.growth = "wikipedia.org"
        self.run_for(60)
        self.assertEqual(len(self.pf.kills), n)


class TogetherTests(Base):
    def test_together_not_counted_reminders_and_end(self):
        self.pf.fg = GD
        self.cmd("/insieme_inizio")
        self.assertIn("👨‍👦 Modalità insieme attiva", self.titles())
        self.run_for(70 * 60)
        self.assertAlmostEqual(self.eng.state["total"], 0)
        self.assertAlmostEqual(self.eng.state["together"], 70 * 60)
        self.assertNotIn("⏰ Tempo concordato terminato", self.titles())
        self.run_for(21 * 60)  # 91 min: primo promemoria su Deck e Telegram
        rem = [p for p in self.ob.parents if "Modalità insieme attiva da" in p[1]]
        self.assertEqual(len(rem), 1)
        self.assertIn("👨‍👦 Modalità insieme", self.titles())
        self.run_for(30 * 60)
        rem = [p for p in self.ob.parents if "Modalità insieme attiva da" in p[1]]
        self.assertEqual(len(rem), 2)
        self.assertEqual(self.titles().count("👨‍👦 Modalità insieme"), 1)  # sulla Deck solo il primo
        self.cmd("/insieme_fine")
        self.run_for(60)
        self.assertAlmostEqual(self.eng.state["total"], 60)

    def test_together_suspends_pause(self):
        self.pf.fg = MC
        self.cmd("/timer_pausa")
        self.cmd("/insieme_inizio")
        self.run_for(300)
        self.assertEqual(self.pf.kills, [])
        self.cmd("/insieme_fine")
        self.run_for(30)
        self.assertEqual(self.pf.kills, [])  # nuova grazia di 60 s
        self.run_for(40)
        self.assertEqual(self.pf.kills, ["class:minecraft"])

    def test_together_closed_at_midnight(self):
        self.cmd("/insieme_inizio")
        self.now = dt.datetime(2026, 11, 20, 0, 0, 5)
        self.eng.tick(self.now, 10)
        self.assertIsNone(self.eng.together())
        self.assertTrue(any("mezzanotte" in p[1] for p in self.ob.parents))


class DayTypeTests(Base):
    def test_weekend_is_festivo_and_override(self):
        self.assertEqual(self.eng.day_type(dt.date(2026, 11, 21)), "festivo")
        self.assertEqual(self.eng.day_type(dt.date(2026, 11, 19)), "feriale")
        r = self.cmd("/timer_considera_oggi_festivo")
        self.assertIn("festivo", r)
        total, _ = self.eng.limits_now(self.now.date())
        self.assertEqual(total.soft, 180)
        self.cmd("/timer_considera_oggi_feriale")
        total, _ = self.eng.limits_now(self.now.date())
        self.assertEqual(total.soft, 120)


class CommandTests(Base):
    def test_limite_imposta_by_steam_name_creates_rule(self):
        r = self.cmd("/limite_imposta hades 45")
        self.assertIn("concordato 45 min, massimo 50 min", r)
        aid = self.eng.limits.find_app("hades")
        self.assertEqual(self.eng.limits.apps[aid].patterns, ["steam:1145360"])
        self.assertTrue(self.saved)
        self.assertTrue(any("Papà" in m[1] for m in self.ob.direct))  # avvisato l'altro genitore

    def test_limite_imposta_variants(self):
        self.cmd("/limite_imposta geometry dash festivo 90 100")
        lim = self.eng.limits.apps["gd"].limits
        self.assertEqual((lim["festivo"].soft, lim["festivo"].hard), (90, 100))
        self.assertEqual(lim["feriale"].soft, 60)
        self.cmd("/limite_imposta totale feriale 100")
        self.assertEqual((self.eng.limits.total["feriale"].soft,
                          self.eng.limits.total["feriale"].hard), (100, 105))
        self.cmd("/limite_rimuovi geometry dash")
        self.assertTrue(self.eng.limits.apps["gd"].limits["feriale"].empty)
        self.assertIn("Uso:", self.cmd("/limite_imposta"))
        self.assertIn("Non trovo", self.cmd("/limite_imposta pippo 30"))

    def test_saved_limits_roundtrip(self):
        self.cmd("/limite_imposta hades 45")
        self.cmd("/configura_aggiungi_sito_crescita https://www.treccani.it/enciclopedia/roma")
        L = self.eng.limits
        L2 = d.Limits(d.load_toml_text(L.to_toml()), 5)
        self.assertEqual(d.limits_diff(L, L2), [])
        self.assertIn("treccani.it", L2.growth_sites)

    def test_growth_sites_commands(self):
        r = self.cmd("/configura_aggiungi_sito_crescita chess.com")
        self.assertIn("aggiunto", r)
        self.assertIn("già", self.cmd("/configura_aggiungi_sito_crescita chess.com"))
        self.cmd("/configura_rimuovi_sito_crescita chess.com")
        self.assertNotIn("chess.com", self.eng.limits.growth_sites)
        self.assertIn("nuvola.madisoft.it", self.cmd("/info_crescita"))
        self.assertIn("nuvola.madisoft.it", self.cmd("/info_scuola"))   # vecchio nome
        self.cmd("/configura_aggiungi_sito_scuola claude.ai")
        self.assertIn("claude.ai", self.eng.limits.growth_sites)

    def test_legacy_scuola_section_and_state(self):
        L = d.Limits(d.load_toml_text('[scuola]\nsiti = ["nuvola.madisoft.it"]'), 5)
        self.assertEqual(L.growth_sites, ["nuvola.madisoft.it"])
        self.assertIn("[crescita]", L.to_toml())
        (self.root / "days").mkdir(exist_ok=True)
        (self.root / "days" / "2026-11-10.json").write_text(
            '{"date": "2026-11-10", "total": 0, "apps": {}, "school": 600}')
        st = self.store.load("2026-11-10")
        self.assertEqual((st["growth"], st["growth_detail"]), (600, {"Scuola": 600}))

    def test_nome_app_and_new_app_alert(self):
        game = d.Foreground("class:game.exe", "game.exe", title="Fortnite")
        self.pf.fg = game
        self.run_for(4 * 60)
        self.assertFalse(any("Nuova app" in p[1] for p in self.ob.parents))
        self.run_for(70)
        alerts = [p for p in self.ob.parents if "Nuova app" in p[1]]
        self.assertEqual(len(alerts), 1)
        self.assertIn("/configura_nome_app class:game.exe Nome", alerts[0][1])
        self.cmd("/configura_nome_app class:game.exe Fortnite")
        self.assertIn("Fortnite — class:game.exe", self.cmd("/info_app"))
        self.cmd("/limite_imposta fortnite 30")
        aid = self.eng.limits.find_app("fortnite")
        self.assertEqual(self.eng.limits.apps[aid].limits["feriale"].soft, 30)
        self.run_for(600)
        self.assertEqual(len([p for p in self.ob.parents if "Nuova app" in p[1]]), 1)

    def test_extra_available_in_monitor(self):
        r = self.cmd("/timer_concedi_extra geometry dash 15")
        self.assertIn("Fatto", r)
        _, apps = self.eng.limits_now(self.now.date())
        self.assertEqual((apps["gd"].soft, apps["gd"].hard), (75, 80))

    def test_help_lists_all_commands(self):
        h = self.cmd("/info_comandi")
        for name, _, _ in d.COMMANDS:
            self.assertIn("/" + name, h)
        self.assertEqual(self.cmd("/start"), h)
        self.assertTrue(all(len(c) <= 32 and c.replace("_", "").isalnum() and c == c.lower()
                            for c, _, _ in d.COMMANDS))


class ReportTests(Base):
    def test_daily_report_at_rollover_and_layout(self):
        self.pf.fg = GD
        self.run_for(30 * 60)
        self.now = dt.datetime(2026, 11, 20, 0, 0, 5)
        self.eng.tick(self.now, 10)
        daily = [p for p in self.ob.parents if p[0].startswith("DeckWatch 19/11")]
        self.assertEqual(len(daily), 1)
        body = daily[0][1]
        self.assertIn("giorno feriale", body)
        self.assertLess(body.index("RIEPILOGO"), body.index("DETTAGLIO SVAGO"))
        self.assertIn("✅ Geometry Dash: 30 min / 1h 00m", body)
        self.assertEqual(len([k for k in self.ob.kid if "giovedì" in k[0]]), 1)

    def test_weekly_on_saturday(self):
        self.pf.fg = GD
        self.run_for(30 * 60)
        self.now = dt.datetime(2026, 11, 20, 15, 0)
        self.run_for(62 * 60)  # venerdì: oltre il concordato di GD
        self.now = dt.datetime(2026, 11, 21, 9, 0)
        self.eng.tick(self.now, 10)
        weekly = [p for p in self.ob.parents if "settimana" in p[0]]
        self.assertEqual(len(weekly), 1)
        self.assertIn("Geometry Dash: 1h 32m — oltre il concordato 1 giorno", weekly[0][1])
        self.now = dt.datetime(2026, 11, 22, 9, 0)
        self.eng.tick(self.now, 10)
        self.assertEqual(len([p for p in self.ob.parents if "settimana" in p[0]]), 1)

    def test_week_so_far_command(self):
        self.pf.fg = MC
        self.run_for(600)
        r = self.cmd("/report_settimana")
        self.assertIn("settimana 14/11 – 19/11 (fin qui)", r)


class MigrationTests(unittest.TestCase):
    def test_legacy_policy_to_limits(self):
        old = d.load_toml_text("""
mode = "monitor"
idle_minutes = 3
[grace]
minutes = 5
relaunch_seconds = 25
[total]
soft = 100
hard = 120
weekend = { soft = 150, hard = 180 }
[apps.geometry_dash]
name = "Geometry Dash"
match = ["steam:322170"]
soft = 45
hard = 60
""")
        pol = d.Policy(old)
        self.assertTrue(pol.legacy)
        L = d.migrate_legacy_limits(old, 5)
        self.assertEqual((L.total["feriale"].soft, L.total["feriale"].hard), (120, 125))
        self.assertEqual((L.total["festivo"].soft, L.total["festivo"].hard), (180, 185))
        self.assertEqual(L.apps["geometry_dash"].limits["festivo"].soft, 60)
        newpol = d.Policy(d.load_toml_text(d.render_policy(d.migrate_policy_values(old))))
        self.assertEqual(newpol.idle_minutes, 2)
        self.assertEqual(newpol.relaunch_seconds, 25)
        self.assertFalse(newpol.legacy)

    def test_example_files_valid(self):
        root = Path(__file__).resolve().parent.parent
        d.Policy(d.load_toml_text((root / "policy.example.toml").read_text()))
        self.assertEqual((root / "policy.example.toml").read_text(), d.render_policy())


def lz4_literals(data: bytes) -> bytes:
    n = len(data)
    if n < 15:
        return bytes([n << 4]) + data
    rest, out = n - 15, bytearray([0xF0])
    while rest >= 255:
        out.append(255)
        rest -= 255
    out.append(rest)
    return bytes(out) + data


class FirefoxTests(unittest.TestCase):
    def test_lz4(self):
        self.assertEqual(d.lz4_block_decompress(b"\x35abc\x03\x00"), b"abcabcabcabc")
        data = b"x" * 300 + b"fine"
        self.assertEqual(d.lz4_block_decompress(lz4_literals(data)), data)

    def test_url_rules(self):
        sites = [d._norm_site(s) for s in d.DEFAULT_GROWTH_SITES]
        ok = ["https://it.wikipedia.org/wiki/Roma", "https://wikipedia.org", "about:newtab",
              "https://nuvola.madisoft.it/login", "https://docs.google.com/document/1"]
        ko = ["https://www.youtube.com/", "https://sites.google.com/view/giochi",
              "https://www.google.com/search?q=snake", "file:///home/deck/x.html",
              "about:reader?url=https%3A%2F%2Fpoki.com%2F"]
        for u in ok:
            self.assertTrue(d.firefox_url_ok(u, sites), u)
        for u in ko:
            self.assertFalse(d.firefox_url_ok(u, sites), u)
        self.assertEqual(d._norm_site("https://www.treccani.it/x"), "treccani.it")
        self.assertEqual(d.growth_site_label("https://it.wikipedia.org/wiki/A", sites), "wikipedia.org")
        self.assertEqual(d.growth_site_label("about:newtab", sites), "Firefox")
        self.assertTrue(d.site_allowed("www.treccani.it", ["treccani.it"]))

    def test_session_reader_all_tabs(self):
        with tempfile.TemporaryDirectory() as t:
            prof = Path(t) / "abc.default"
            (prof / "sessionstore-backups").mkdir(parents=True)
            (prof / "prefs.js").write_text("")
            os.symlink("127.0.0.1:+1234", prof / "lock")
            sess = {"selectedWindow": 1, "windows": [{"selected": 2, "tabs": [
                {"entries": [{"url": "https://it.wikipedia.org/wiki/A"}], "index": 1},
                {"entries": [{"url": "https://www.youtube.com/"},
                             {"url": "https://it.wikipedia.org/wiki/B"}], "index": 2}]}]}
            raw = json.dumps(sess).encode()
            f = prof / "sessionstore-backups" / "recovery.jsonlz4"
            f.write_bytes(b"mozLz40\0" + len(raw).to_bytes(4, "little") + lz4_literals(raw))
            fs = d.FirefoxSession()
            fs.roots = [Path(t)]
            urls, n, active = fs.read()
            self.assertEqual(n, 1)
            self.assertEqual(active, "https://it.wikipedia.org/wiki/B")
            self.assertEqual(urls, ["https://it.wikipedia.org/wiki/A", "https://it.wikipedia.org/wiki/B"])


if __name__ == "__main__":
    unittest.main()
