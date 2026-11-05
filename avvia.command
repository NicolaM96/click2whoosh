#!/bin/zsh
# Doppio clic per avviare click2whoosh (si apre nel Terminale, che ha i permessi).
# Al primo avvio su un Mac nuovo prepara da solo l'ambiente Python.
cd "$(dirname "$0")" || exit 1

# Una .venv copiata da un altro Mac di solito non funziona: si ricrea.
if [[ -d .venv ]] && ! .venv/bin/python -c 'import bleak, rumps, Quartz' 2>/dev/null; then
  echo "L'ambiente Python in .venv non funziona su questo Mac: lo ricreo."
  rm -rf .venv
fi

if [[ ! -x .venv/bin/python ]]; then
  # Serve Python 3.11 o più recente: quello incluso in macOS (3.9) non basta.
  PY=""
  for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
    if command -v $candidate >/dev/null && $candidate -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
      PY=$candidate
      break
    fi
  done
  if [[ -z $PY ]]; then
    echo "Serve Python 3.11 o più recente. Scaricalo da https://www.python.org/downloads/macos/ e riapri questo file."
    read -k1 "?Premi un tasto per chiudere."
    exit 1
  fi
  echo "Primo avvio: preparo l'ambiente con $PY (serve internet, circa un minuto)..."
  $PY -m venv .venv && .venv/bin/pip install -q -r requirements.txt || { read -k1 "?Installazione fallita. Premi un tasto."; exit 1; }
fi

exec .venv/bin/python src/app.py
