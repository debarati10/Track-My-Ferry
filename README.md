# Track My Ferry

NYC Ferry tells you when a boat is scheduled, but not where it actually is.
This app reads the ferries' live AIS signals, figures out which trip each boat is running, and shows them on a map of the harbor.
Tap a landing to see when the next boat will really arrive (as a time range) and how likely it is to be full.
There's also a demo mode with simulated boats, so you can try it at any hour.

<img width="2880" height="1800" alt="Screenshot 2026-10-13 at 4 08 56 AM" src="https://github.com/user-attachments/assets/3658ac34-4012-4229-b1e5-168f6ede5def" />


## Getting started

You'll need Python 3.11+ and Node.js 18+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cd web && npm install && npm run build && cd ..
```

Demo with simulated boats:

```bash
trackmyferry run --mode sim --speedup 5
```

Live NYC Ferry boats (get a free key at https://aisstream.io):

```bash
export AISSTREAM_API_KEY=your_key
trackmyferry run --mode live
```

Then open http://localhost:8000. On a Mac, if you see a certificate error in live mode, run
`"/Applications/Python 3.11/Install Certificates.command"` once.
