"""PC -> WhatsApp: lê estado/avisos.jsonl do repositório T0LD-DEV/yt-comentarios e manda os avisos novos.
Roda pela tarefa do Windows 'Comentarios nuvem - avisos WhatsApp' a cada 10 min (sem LLM).
Só avisa; quem comenta é o GitHub Actions. Com o PC desligado os avisos ficam guardados e saem quando ligar.
"""
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

URL = "https://api.github.com/repos/T0LD-DEV/yt-comentarios/contents/estado/avisos.jsonl?ref=main"
CTRL = Path(os.environ.get("LOCALAPPDATA", ".")) / "hermes" / "yt_comentarios_avisos_enviados.json"
HERMES = shutil.which("hermes") or r"C:\Users\bruno\AppData\Local\hermes\hermes-agent\.hermes\bin\hermes.exe"


def main():
    r = urllib.request.Request(URL, headers={"Accept": "application/vnd.github.raw", "User-Agent": "t0ld-avisos"})
    try:
        linhas = urllib.request.urlopen(r, timeout=30).read().decode("utf-8").splitlines()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return
        raise
    avisos = [json.loads(l) for l in linhas if l.strip()]
    enviados = set(json.loads(CTRL.read_text(encoding="utf-8"))) if CTRL.exists() else set()
    novos = [a for a in avisos if a["id"] not in enviados]
    if not novos:
        return
    msg = "🤖 Comentários (nuvem)\n\n" + "\n\n".join(a["texto"] for a in novos[-15:])
    if "--dry" in sys.argv:
        print(msg); return
    p = subprocess.run([HERMES, "send", "-t", "whatsapp", msg], capture_output=True, text=True, timeout=180)
    print("hermes send rc", p.returncode, (p.stdout or p.stderr)[:200])
    if p.returncode == 0:
        enviados |= {a["id"] for a in novos}
        CTRL.write_text(json.dumps(sorted(enviados)[-2000:]), encoding="utf-8")


if __name__ == "__main__":
    main()
