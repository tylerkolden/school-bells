# Bell System project context

**Project:** `bell-system` — a scheduled multicast paging transmitter for a Catholic K-12 school. It runs unattended on a Raspberry Pi on the phone VLAN and sends audio to Yealink T31P phones and an Algo 8186 horn speaker.

## Hard constraints — never violate these

- Python 3.11+, standard library preferred. Third-party dependencies must be justified. Approved project dependencies are Pydantic v2, APScheduler, FastAPI, Uvicorn, Jinja2, PyYAML, ruamel.yaml, and pytest/test tooling.
- The default receivers use `239.255.255.255:601` but can be modified. The zone is identified by a Poly group/channel number: 23 Indoors, 24 Outdoors, 25 Everywhere but can also be modified.
- The Algo 8186 uses **Poly Group Page**, not Regular RTP. Its channel header layout must come from a live capture. Never infer or invent it. An uncalibrated Poly transmitter must raise.
- Audio is G.711 mu-law (PCMU), 8 kHz mono, RTP payload type 0, in 20 ms/160-byte packets.
- All scheduling uses `America/Denver` local wall-clock time. DST must not shift bell times.
- Safety guards are mandatory and may not be bypassed for convenience.

## Style

Use type hints, structured logging rather than prints in services, no bare `except`, Ruff-clean code, and pytest coverage for logic.
