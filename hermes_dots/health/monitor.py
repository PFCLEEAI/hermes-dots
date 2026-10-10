"""Injected probes, immediate evidence, and quiet-hour-safe owner alerts."""

from hermes_dots.engine.support import event, now, queue_ping, release_pings, stamp


PROBE_NAMES = ("mail", "slack", "docker", "model", "scheduler", "disk")


class HealthMonitor:
    def __init__(self, ledger, settings, probes, *, clock=None, evidence=None, owner_channel="owner"):
        self.ledger = ledger
        self.settings = settings
        self.probes = dict(probes)
        self.clock = clock
        self.evidence = evidence
        self.owner_channel = owner_channel

    def tick(self):
        for name in PROBE_NAMES:
            try:
                result = self.probes[name]()
                if isinstance(result, dict):
                    ok = result.get("ok") is True
                    detail = str(result.get("detail", ""))
                else:
                    ok = result is True
                    detail = "" if ok else "Probe did not confirm access"
            except Exception as error:
                ok, detail = False, f"{type(error).__name__}: {error}"
            # Only failures/recoveries ping, but every tick invokes every probe.
            with self.ledger.transaction():
                previous = self.ledger.connection.execute("SELECT ok FROM health_status WHERE name=?", (name,)).fetchone()
                record = event(self.ledger, "health_probe", {"name": name, "ok": ok, "detail": detail}, evidence=self.evidence)
                detail = record.payload["detail"]
                self.ledger.connection.execute(
                    "INSERT INTO health_status VALUES (?,?,?,?) ON CONFLICT(name) DO UPDATE SET ok=excluded.ok,detail=excluded.detail,checked_at=excluded.checked_at",
                    (name, ok, detail, stamp(now(self.clock))),
                )
                if not ok and (previous is None or previous["ok"]):
                    queue_ping(self.ledger, self.settings, self.clock, {"kind": "health", "probe": name, "detail": detail})
        return self.status()

    def status(self):
        with self.ledger.transaction():
            return {row["name"]: {"ok": bool(row["ok"]), "detail": row["detail"], "checked_at": row["checked_at"]}
                    for row in self.ledger.connection.execute("SELECT * FROM health_status ORDER BY name")}

    def release_pings(self):
        return release_pings(self.ledger, self.settings, self.clock, self.owner_channel)
