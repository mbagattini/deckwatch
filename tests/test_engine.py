import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import deckwatch as d  # noqa: E402

POLICY = """
mode = "enforce"
idle_minutes = 3
lock_grace_seconds = 60
[grace]
minutes = 5
warn_before = [10, 5, 1]
relaunch_seconds = 20
[total]
soft = 100
hard = 120
weekend = { hard = 180 }
[apps.gd]
name = "Geometry Dash"
match = ["steam:322170"]
soft = 45
hard = 60
[apps.browser]
name = "Browser"
match = ["class:firefox"]
"""

GD = d.Foreground("steam:322170", "Geometry Dash", steam_appid="322170")
FF = d.Foreground("class:firefox", "firefox", window=5)
STEAM = d.Foreground("class:steam", "steam", window=7)
SHELL = d.Foreground("class:plasmashell", "plasmashell")


class FakePlatform:
    def __init__(self):
        self.fg = None
        self.idle = 0.0
        self.notes, self.popups, self.kills, self.steam_kills = [], [], [], []
        self.games = {}
        self.powered_off = False

    def foreground(self): return self.fg
    def idle_seconds(self): return self.idle
    def notify(self, t, b, urgent=False): self.notes.append((t, b, urgent))
    def popup(self, text): self.popups.append(text)
    def kill(self, fg): self.kills.append(fg.key); self.fg = None
    def kill_steam(self, appid): self.steam_kills.append(appid); self.games.pop(appid, None)
    def running_steam_games(self): return dict(self.games)
    def steam_name(self, appid): return {"322170": "Geometry Dash"}.get(appid)
    def poweroff(self): self.powered_off = True


class FakeOutbox:
    def __init__(self): self.parents, self.kid, self.direct = [], [], []
    def to_parents(self, text, subject="DeckWatch"): self.parents.append((subject, text))
    def to_kid(self, text, subject): self.kid.append((subject, text))
    def enqueue(self, ch, to, subj, body): self.direct.append((to, body))


class Base(unittest.TestCase):
    policy_text = POLICY

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.pf = FakePlatform()
        self.ob = FakeOutbox()
        self.cfg = d.Config(tg_token="x", tg_parents=[1, 2])
        self.store = d.DayStore(root / "days")
        self.eng = d.Engine(self.pf, self.store, self.ob, self.cfg,
                            d.Policy(d.load_toml_text(self.policy_text)),
                            d.JsonFile(root / "ov.json"))
        self.now = dt.datetime(2026, 11, 19, 15, 0, 0)  # giovedì
        self.eng.start(self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def run_for(self, seconds, step=5):
        t = 0
        while t < seconds:
            self.now += dt.timedelta(seconds=step)
            self.eng.tick(self.now, step)
            t += step


class CountingTests(Base):
    def test_counts_only_active_foreground(self):
        self.pf.fg = FF
        self.run_for(60)
        self.assertAlmostEqual(self.eng.state["total"], 60)
        self.pf.idle = 400  # inattivo
        self.run_for(60)
        self.assertAlmostEqual(self.eng.state["total"], 60)
        self.pf.idle = 0
        self.pf.fg = SHELL  # escluso
        self.run_for(60)
        self.assertAlmostEqual(self.eng.state["total"], 60)
        self.pf.fg = None
        self.run_for(60)
        self.assertAlmostEqual(self.eng.state["total"], 60)

    def test_usage_grouped_by_app_rule(self):
        self.pf.fg = GD
        self.run_for(120)
        self.assertAlmostEqual(self.eng.usage_by_app()["gd"], 120)


class AppLimitTests(Base):
    def test_soft_warn_hard_grace_kill_and_relaunch(self):
        self.pf.fg = GD
        self.run_for(45 * 60)
        self.assertTrue(any("soglia" in n[0] for n in self.pf.notes))
        self.run_for(10 * 60)
        self.assertTrue(any("mancano" in n[0] for n in self.pf.notes))
        self.run_for(5 * 60)  # 60 min: tempo finito
        self.assertTrue(any("tempo finito" in n[0] for n in self.pf.notes))
        self.assertEqual(self.pf.kills, [])
        self.run_for(4 * 60)  # in grazia
        self.assertEqual(self.pf.kills, [])
        self.run_for(65)  # grazia scaduta → chiusura immediata
        self.assertEqual(self.pf.kills, ["steam:322170"])
        self.assertTrue(any("Chiuso" in p[1] for p in self.ob.parents))
        # riapre: avviso, poi chiusura dopo 20 s
        self.pf.fg = GD
        self.run_for(5)
        self.assertEqual(len(self.pf.kills), 1)
        self.assertTrue(any("non consentito" in n[0] for n in self.pf.notes))
        self.run_for(20)
        self.assertEqual(len(self.pf.kills), 2)
        # le altre app restano usabili
        self.pf.fg = FF
        self.run_for(60)
        self.assertEqual(len(self.pf.kills), 2)

    def test_background_steam_game_killed(self):
        self.pf.fg = GD
        self.run_for(66 * 60)
        self.pf.fg = FF
        self.pf.games = {"322170": [123]}
        self.run_for(30)
        self.assertIn("322170", self.pf.steam_kills)

    def test_extra_from_telegram_unblocks(self):
        self.pf.fg = GD
        self.run_for(66 * 60)
        self.assertEqual(len(self.pf.kills), 1)
        reply = self.eng.handle_command(1, "Papà", "/extra gd 15", self.now.timestamp(), self.now)
        self.assertIn("Fatto", reply)
        self.assertTrue(any("Papà" in m[1] for m in self.ob.direct))  # avvisato l'altro genitore
        self.pf.fg = GD
        self.run_for(10 * 60)
        self.assertEqual(len(self.pf.kills), 1)
        self.run_for(10 * 60)  # supera i nuovi 75 min + grazia
        self.assertGreaterEqual(len(self.pf.kills), 2)


class TotalAndLockTests(Base):
    def test_total_blocks_non_allowed_only(self):
        self.pf.fg = FF
        self.run_for(126 * 60)
        self.assertIn("class:firefox", self.pf.kills)
        n = len(self.pf.kills)
        self.pf.fg = STEAM
        self.run_for(120)
        self.assertEqual(len(self.pf.kills), n)

    def test_lock_and_unlock(self):
        self.pf.fg = FF
        self.run_for(10)
        self.eng.handle_command(1, "Mamma", "/stop", self.now.timestamp(), self.now)
        self.run_for(30)
        self.assertEqual(self.pf.kills, [])  # grazia del blocco
        self.assertTrue(any("pausa" in n[0] for n in self.pf.notes))
        self.run_for(40)
        self.assertEqual(self.pf.kills, ["class:firefox"])
        self.eng.handle_command(1, "Mamma", "/sblocca", self.now.timestamp(), self.now)
        self.pf.fg = FF
        self.run_for(60)
        self.assertEqual(self.pf.kills, ["class:firefox"])

    def test_timed_lock_expires(self):
        self.eng.handle_command(1, "Mamma", "/stop 10", self.now.timestamp(), self.now)
        self.assertTrue(self.eng.locked(self.now))
        self.assertFalse(self.eng.locked(self.now + dt.timedelta(minutes=11)))

    def test_stale_poweroff_ignored(self):
        old = (self.now - dt.timedelta(hours=2)).timestamp()
        r = self.eng.handle_command(1, "Papà", "/spegni", old, self.now)
        self.assertIn("ignorato", r)


class ShutdownPolicy(Base):
    policy_text = POLICY.replace("relaunch_seconds = 20",
                                 "relaunch_seconds = 20\non_total_exceeded = \"shutdown\"")

    def test_shutdown_on_total(self):
        self.pf.fg = FF
        self.run_for(126 * 60)
        self.assertTrue(self.pf.powered_off)


class ReportTests(Base):
    def test_daily_report_sent_at_rollover(self):
        self.pf.fg = GD
        self.run_for(30 * 60)
        self.now = self.now.replace(hour=23, minute=59, second=50)
        self.eng.tick(self.now, 5)
        self.assertEqual(self.ob.parents, [])  # niente report in serata
        self.now = dt.datetime(2026, 11, 20, 0, 0, 5)
        self.eng.tick(self.now, 5)
        self.assertEqual(self.eng.state["date"], "2026-11-20")
        self.assertEqual(len(self.ob.parents), 1)
        self.assertEqual(len(self.ob.kid), 1)
        body = self.ob.parents[0][1]
        self.assertLess(body.index("RIEPILOGO"), body.index("DETTAGLIO"))
        self.assertIn("✅ Geometry Dash: 30 min / 1h 00m", body)

    def test_report_shows_overs_for_kid(self):
        self.pf.fg = GD
        self.run_for(64 * 60)
        subj, body = d.build_report(self.eng.state, self.eng.policy, "kid")
        self.assertIn("❌ Geometry Dash: 1h 04m / 1h 00m (+4 min)", body)
        self.assertIn("oltre il tempo concordato per: Geometry Dash", body)
        self.assertNotIn("EVENTI", body)

    def test_missed_report_sent_at_next_start(self):
        self.pf.fg = FF
        self.run_for(600)
        self.eng.save()
        eng2 = d.Engine(self.pf, self.store, self.ob, self.cfg, self.eng.policy, self.eng.overrides)
        eng2.start(dt.datetime(2026, 11, 21, 9, 0))
        daily = [p for p in self.ob.parents if p[0].startswith("DeckWatch 19/11")]
        self.assertEqual(len(daily), 1)
        self.assertIn("giovedì", daily[0][1])

    def test_restart_keeps_counters(self):
        self.pf.fg = FF
        self.run_for(600)
        self.eng.save()
        eng2 = d.Engine(self.pf, self.store, self.ob, self.cfg, self.eng.policy, self.eng.overrides)
        eng2.start(self.now)
        self.assertAlmostEqual(eng2.state["total"], 600)


MONITOR = POLICY.replace('mode = "enforce"', 'mode = "monitor"')


class MonitorModeTests(Base):
    policy_text = MONITOR

    def test_no_kill_but_simulation_and_warnings(self):
        self.pf.fg = GD
        self.run_for(70 * 60)
        self.assertEqual(self.pf.kills, [])
        self.assertTrue(any("tempo finito" in n[0] for n in self.pf.notes))
        self.assertTrue(any("solo un avviso" in n[1] for n in self.pf.notes))
        sims = [e for e in self.eng.state["events"] if e[2] == "sim"]
        self.assertEqual(len(sims), 1)
        body = d.build_report(self.eng.state, self.eng.policy, "parents")[1]
        self.assertIn("(simulazione) Geometry Dash sarebbe stato chiuso", body)

    def test_lock_still_works(self):
        self.pf.fg = FF
        self.eng.handle_command(1, "Mamma", "/stop", self.now.timestamp(), self.now)
        self.run_for(70)
        self.assertEqual(self.pf.kills, ["class:firefox"])

    def test_extra_disabled(self):
        r = self.eng.handle_command(1, "Papà", "/extra 30", self.now.timestamp(), self.now)
        self.assertIn("monitor", r)
        self.assertNotIn("/extra", self.eng.help_text())


class WeeklyTests(Base):
    def test_weekly_sent_once_on_saturday(self):
        # giovedì 19 e venerdì 20 con attività
        self.pf.fg = GD
        self.run_for(30 * 60)
        self.now = dt.datetime(2026, 11, 20, 15, 0)
        self.run_for(70 * 60)
        self.assertEqual(len([p for p in self.ob.parents if p[0].startswith("DeckWatch 19/11")]), 1)
        weekly_before = [p for p in self.ob.parents if "settimana" in p[0]]
        # sabato mattina: report di venerdì + settimanale
        self.now = dt.datetime(2026, 11, 21, 9, 0)
        self.eng.tick(self.now, 5)
        weekly = [p for p in self.ob.parents if "settimana" in p[0]]
        self.assertEqual(len(weekly) - len(weekly_before), 1)
        body = weekly[-1][1]
        self.assertIn("settimana 14/11 – 20/11", body)
        self.assertIn("Geometry Dash: 1h 35m — oltre il limite 1 giorno", body)
        self.assertIn("❌ ven 20/11", body)
        self.assertTrue(any("settimana" in k[0] for k in self.ob.kid))
        # domenica: non lo rimanda
        self.now = dt.datetime(2026, 11, 22, 9, 0)
        self.eng.tick(self.now, 5)
        self.assertEqual(len([p for p in self.ob.parents if "settimana" in p[0]]), len(weekly))

    def test_weekly_after_deck_off_all_weekend(self):
        self.pf.fg = FF
        self.run_for(600)
        self.eng.save()
        eng2 = d.Engine(self.pf, self.store, self.ob, self.cfg, self.eng.policy, self.eng.overrides)
        eng2.start(dt.datetime(2026, 11, 23, 8, 0))  # lunedì
        self.assertEqual(len([p for p in self.ob.parents if "settimana" in p[0]]), 1)

    def test_last_friday(self):
        self.assertEqual(d.last_friday_before(dt.date(2026, 11, 21)), dt.date(2026, 11, 20))
        self.assertEqual(d.last_friday_before(dt.date(2026, 11, 20)), dt.date(2026, 11, 13))
        self.assertEqual(d.last_friday_before(dt.date(2026, 11, 23)), dt.date(2026, 11, 20))


class PolicyTests(unittest.TestCase):
    def test_weekend_and_exceptions(self):
        p = d.Policy(d.load_toml_text(POLICY + """
[exceptions.2026-11-19]
extra = 30
apps = { gd = 15 }
[exceptions.2026-11-20]
unlimited = true
"""))
        t, a = p.day_limits(dt.date(2026, 11, 21))
        self.assertEqual(t.hard, 180)
        t, a = p.day_limits(dt.date(2026, 11, 19))
        self.assertEqual((t.soft, t.hard, a["gd"].hard), (130, 150, 75))
        t, a = p.day_limits(dt.date(2026, 11, 20))
        self.assertIsNone(t.hard)

    def test_errors_are_readable(self):
        for bad in ("[total]\nhard = \"due ore\"", "[apps.x]\nhard = 3",
                    "[total]\nsatu = { hard = 3 }", "[exceptions.2026-13-40]\nextra = 1",
                    "lok = true"):
            with self.assertRaises(d.PolicyError):
                d.Policy(d.load_toml_text(bad))

    def test_example_policy_is_valid(self):
        d.load_policy_file(Path(__file__).resolve().parent.parent / "policy.example.toml")


if __name__ == "__main__":
    unittest.main()
