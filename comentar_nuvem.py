"""Comentário automático (dono do canal) ~1 min depois que cada vídeo/Short sai, em todos os canais T0LD.

Roda no GitHub Actions (cron a cada 10 min). Cada execução:
  1. lê os uploads de cada canal pela YouTube Data API (OAuth do dono vê os agendados);
  2. vídeos que publicam nos próximos JANELA_MIN minutos: espera T+60 s, confere que ficou público e comenta;
  3. vídeos públicos recentes (até ATRASO_MAX_H) ainda sem comentário do dono: comenta já (recupera atrasos do cron);
  4. grava estado/ (comentados, status por canal, avisos.jsonl p/ o WhatsApp) e faz commit.
Texto: Claude Haiku (ANTHROPIC_API_KEY), com links obrigatórios conferidos no código; Achadinhos é montado sem IA.
A API do YouTube não fixa comentários (decisão do Bruno: só nuvem, sem fixar).

Segredos (env): GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, YT_TOKENS (JSON {canal: refresh_token}), ANTHROPIC_API_KEY.
Teste local: python comentar_nuvem.py --dry   (não comenta nem grava)
"""
import datetime as dt
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).parent
EST = RAIZ / "estado"
CANAIS = json.loads((RAIZ / "canais.json").read_text(encoding="utf-8"))
DRY = "--dry" in sys.argv
JANELA_MIN = int(os.environ.get("JANELA_MIN", "40"))      # olha vídeos que saem até daqui a N min
ATRASO_MAX_H = 48                                          # comenta atrasado até 48 h depois
INICIO = os.environ.get("INICIO_UTC", "2026-11-05T22:00:00+00:00")   # não mexe em vídeos publicados antes disso
ESTOQUE_MIN_H = 24
MODELO = os.environ.get("MODELO", "claude-haiku-5-5")
BRT = dt.timezone(dt.timedelta(hours=-3))
UTC = dt.timezone.utc
URL_RE = re.compile(r"https?://\S+")


def agora():
    return dt.datetime.now(UTC)


def log(*a):
    print(f"[{agora().astimezone(BRT):%d/%m %H:%M:%S}]", *a, flush=True)


def jload(p, padrao):
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else padrao


def jsave(p, d):
    if DRY:
        return
    Path(p).parent.mkdir(exist_ok=True)
    Path(p).write_text(json.dumps(d, indent=1, ensure_ascii=False), encoding="utf-8")


def iso(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def brt(t):
    return t.astimezone(BRT).strftime("%d/%m %H:%M")


# ------------------------------------------------------------------ avisos (lidos no PC e mandados no WhatsApp)
def avisar(texto, chave=None, a_cada_h=0):
    """Acrescenta em estado/avisos.jsonl. chave+a_cada_h evita repetir o mesmo aviso."""
    ctrl = jload(EST / "avisos_ctrl.json", {})
    if chave and a_cada_h:
        ult = ctrl.get(chave)
        if ult and agora() - iso(ult) < dt.timedelta(hours=a_cada_h):
            return
        ctrl[chave] = agora().isoformat()
        jsave(EST / "avisos_ctrl.json", ctrl)
    log("AVISO:", texto)
    if DRY:
        return
    EST.mkdir(exist_ok=True)
    with (EST / "avisos.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": f"{time.time():.3f}", "ts": agora().isoformat(), "texto": texto}, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------ YouTube
class YT:
    def __init__(self, canal, refresh):
        self.canal, self.refresh, self.at, self.exp = canal, refresh, None, 0

    def token(self):
        if not self.at or time.time() > self.exp - 60:
            d = urllib.parse.urlencode(dict(client_id=os.environ["GOOGLE_CLIENT_ID"], client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
                                            refresh_token=self.refresh, grant_type="refresh_token")).encode()
            r = json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", d, timeout=30))
            self.at, self.exp = r["access_token"], time.time() + r.get("expires_in", 3600)
        return self.at

    def req(self, metodo, path, corpo=None, **q):
        url = "https://www.googleapis.com/youtube/v3/" + path + "?" + urllib.parse.urlencode(q)
        dados = json.dumps(corpo).encode() if corpo is not None else None
        for tent in range(3):
            r = urllib.request.Request(url, data=dados, method=metodo, headers={
                "Authorization": "Bearer " + self.token(), "Content-Type": "application/json"})
            try:
                return json.load(urllib.request.urlopen(r, timeout=40))
            except urllib.error.HTTPError as e:
                corpo_erro = e.read().decode("utf-8", "ignore")
                if e.code >= 500 and tent < 2:
                    time.sleep(5 * (tent + 1)); continue
                raise RuntimeError(f"HTTP {e.code} {path}: {corpo_erro[:300]}")
            except (urllib.error.URLError, TimeoutError) as e:
                if tent < 2:
                    time.sleep(5 * (tent + 1)); continue
                raise

    def videos_recentes(self, cid):
        ids, tok = [], None
        for _ in range(2):                         # até 100 uploads (agendados aparecem primeiro)
            q = dict(part="contentDetails", playlistId="UU" + cid[2:], maxResults=50)
            if tok: q["pageToken"] = tok
            r = self.req("GET", "playlistItems", **q)
            ids += [i["contentDetails"]["videoId"] for i in r.get("items", []) if i["contentDetails"]["videoId"] not in ids]
            tok = r.get("nextPageToken")
            if not tok: break
        out = []
        for i in range(0, len(ids), 50):
            out += self.req("GET", "videos", part="status,snippet,contentDetails", id=",".join(ids[i:i + 50])).get("items", [])
        return out

    def status(self, vid):
        it = self.req("GET", "videos", part="status,snippet", id=vid).get("items", [])
        return it[0] if it else None

    def comentario_do_dono(self, vid, cid):
        try:
            r = self.req("GET", "commentThreads", part="snippet", videoId=vid, maxResults=50, order="time")
        except RuntimeError as e:
            if "commentsDisabled" in str(e):
                return "desativado"
            raise
        for t in r.get("items", []):
            s = t["snippet"]["topLevelComment"]["snippet"]
            if s.get("authorChannelId", {}).get("value") == cid:
                return t["id"]
        return None

    def comentar(self, vid, texto):
        r = self.req("POST", "commentThreads", part="snippet",
                     corpo={"snippet": {"videoId": vid, "topLevelComment": {"snippet": {"textOriginal": texto}}}})
        return r["id"]


def eh_short(v):
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", v.get("contentDetails", {}).get("duration", ""))
    if not m:
        return False
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s <= 180


# ------------------------------------------------------------------ texto
def links_da_descricao(c, desc):
    pads = c.get("links_descricao") or []
    linhas = []
    for l in desc.splitlines():
        l = l.strip()
        if l and URL_RE.search(l) and any(p in l.lower() for p in pads):
            linhas.append(l)
    return linhas


def texto_achadinhos(c, v):
    prods = [l for l in links_da_descricao(c, v["snippet"].get("description", "")) if "t.me/" not in l]
    nums = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    corpo = "\n".join(f"{nums[i] if i < 10 else str(i + 1) + '.'} {l}" for i, l in enumerate(prods))
    return ("🛒 Links dos produtos do vídeo:\n\n" + corpo +
            "\n\n📲 Ofertas todo dia no Telegram: https://t.me/achadinhostold"
            "\n\nLinks de afiliado: você paga o mesmo preço e ajuda o canal. Preços podem mudar.")


def haiku(prompt):
    corpo = json.dumps({"model": MODELO, "max_tokens": 1200, "messages": [{"role": "user", "content": prompt}]}).encode()
    for tent in range(3):
        r = urllib.request.Request("https://api.anthropic.com/v1/messages", data=corpo, headers={
            "x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            d = json.load(urllib.request.urlopen(r, timeout=60))
            txt = "".join(b.get("text", "") for b in d["content"]).strip()
            if os.environ.get("DEBUG_HAIKU"):
                log("haiku stop:", d.get("stop_reason"), d.get("usage", {}).get("output_tokens"), repr(txt[-60:]))
            if d.get("stop_reason") != "end_turn" or not txt:
                raise RuntimeError(f"resposta incompleta: {d.get('stop_reason')}")
            return txt
        except Exception as e:
            log("haiku falhou:", e)
            time.sleep(4 * (tent + 1))
    return None


def texto_reserva(c):
    ex = c.get("exemplos")
    if isinstance(ex, dict):
        return "\n\n".join(random.choice(ex[k]) for k in ("aberturas", "perguntas", "convites") if ex.get(k))
    if isinstance(ex, list) and ex:
        return random.choice(ex)
    return ""


def gerar_texto(slug, c, v, recentes):
    if c.get("formato_fixo"):
        return texto_achadinhos(c, v)
    sn = v["snippet"]
    obrig = links_da_descricao(c, sn.get("description", ""))
    for l in c.get("links_fixos") or []:          # link fixo só se a descrição já não trouxer o mesmo link
        u = URL_RE.search(l).group(0)
        if not any(u in x for x in obrig):
            obrig.append(l)
    links_obrig = [URL_RE.search(l).group(0).rstrip(").,") for l in obrig]
    prompt = f"""You write the channel owner's first comment on a just-published YouTube {'Short' if eh_short(v) else 'video'}.
Channel: {c['nome']}. Language: {'Brazilian Portuguese' if c['idioma'] == 'pt-BR' else 'English'}.
Style: {c['estilo']}
Video title: {sn.get('title', '')}
Video description (excerpt): {sn.get('description', '')[:1200]}
Examples of the channel's tone (write a NEW comment; never reuse their sentences): {json.dumps(c.get('exemplos', ''), ensure_ascii=False)[:1500]}
Recent comments already used (avoid repeating their wording): {json.dumps(recentes[-6:], ensure_ascii=False)[:1200]}
{('Include these lines/links exactly as written: ' + json.dumps(obrig, ensure_ascii=False)) if obrig else 'Do not include any links.'}
Rules: written as the channel owner, natural and specific to THIS video, 2-4 short lines (max 450 characters plus the required links), 1-3 emojis, no hashtags, no quotation marks around the comment, never invent facts, prices or links, do not mention the description or other videos. Output only the comment text."""
    t = haiku(prompt)
    if t:
        t = t.strip().strip('"').strip()
    if not t or len(t) > 1500:
        t = texto_reserva(c)
    falt = [l for l, u in zip(obrig, links_obrig) if u not in t]
    if falt:
        t = t.rstrip() + "\n\n" + "\n".join(falt)
    return t.strip()


# ------------------------------------------------------------------ fluxo
def comentar_video(slug, c, yt, v, feitos):
    vid = v["id"]
    ja = yt.comentario_do_dono(vid, c["canal_id"])
    if ja == "desativado":
        feitos[vid] = {"canal": slug, "quando": agora().isoformat(), "pulado": "comentarios desativados"}
        avisar(f"⚠️ {c['nome']}: comentários desativados em https://youtu.be/{vid} (não comentei)")
        return False
    if ja:
        feitos[vid] = {"canal": slug, "quando": agora().isoformat(), "comment_id": ja, "existente": True}
        log(slug, vid, "já tinha comentário do dono")
        return False
    recentes = [x.get("texto", "") for x in feitos.values() if x.get("canal") == slug and x.get("texto")]
    texto = gerar_texto(slug, c, v, recentes)
    if DRY:
        log(f"[dry] {slug} {vid} comentaria:\n{texto}\n")
        return True
    cid = yt.comentar(vid, texto)
    feitos[vid] = {"canal": slug, "quando": agora().isoformat(), "comment_id": cid, "texto": texto,
                   "titulo": v["snippet"].get("title", "")}
    log(slug, vid, "COMENTADO", cid)
    return True


def main():
    tokens = json.loads(os.environ["YT_TOKENS"])
    feitos = jload(EST / "comentados.json", {})
    status = jload(EST / "status.json", {})
    inicio = iso(INICIO)
    agenda = []          # (quando, slug, video)
    yts, futs = {}, {}
    for slug, c in CANAIS.items():
        if slug not in tokens:
            avisar(f"⚠️ {c['nome']}: sem token do YouTube no GitHub (YT_TOKENS)", chave=f"tok_{slug}", a_cada_h=12)
            continue
        yt = yts[slug] = YT(slug, tokens[slug])
        try:
            vs = yt.videos_recentes(c["canal_id"])
        except Exception as e:
            msg = str(e)
            log(slug, "ERRO lendo vídeos:", msg[:300])
            dica = " (autorização revogada/expirada: refazer OAuth)" if "invalid_grant" in msg or "401" in msg else ""
            avisar(f"❌ {c['nome']}: erro ao ler vídeos{dica}: {msg[:160]}", chave=f"err_{slug}", a_cada_h=6)
            continue
        futuros = []
        for v in vs:
            if v["snippet"].get("liveBroadcastContent") in ("live", "upcoming"):
                continue
            st = v["status"]
            if st["privacyStatus"] != "public" and st.get("publishAt"):
                q = iso(st["publishAt"])
                if q > agora():
                    futuros.append(q)
                if q <= agora() + dt.timedelta(minutes=JANELA_MIN) and v["id"] not in feitos:
                    agenda.append((max(q, agora() - dt.timedelta(minutes=1)), slug, v))
            elif st["privacyStatus"] == "public" and v["id"] not in feitos:
                pub = iso(v["snippet"]["publishedAt"])
                if pub >= inicio and agora() - pub < dt.timedelta(hours=ATRASO_MAX_H):
                    agenda.append((pub, slug, v))
        futuros.sort()
        futs[slug] = futuros
        status[slug] = {"nome": c["nome"], "proximo": futuros[0].isoformat() if futuros else None,
                        "ultimo_agendado": futuros[-1].isoformat() if futuros else None,
                        "agendados": len(futuros)}
        estoque_h = (futuros[-1] - agora()).total_seconds() / 3600 if futuros else 0
        if estoque_h < ESTOQUE_MIN_H:
            fim = f"o último agendado sai {brt(futuros[-1])}" if futuros else "não há NENHUM vídeo agendado"
            avisar(f"⏳ {c['nome']}: estoque no YouTube abaixo de 24 h — {fim}. Agende mais para não ficar lacuna.",
                   chave=f"estoque_{slug}", a_cada_h=12)

    agenda.sort(key=lambda x: x[0])
    log(f"{len(agenda)} vídeo(s) para comentar nesta execução:", [(brt(q), s, v['id']) for q, s, v in agenda])
    jsave(EST / "status.json", status)
    for quando, slug, v in agenda:
        c, yt, vid = CANAIS[slug], yts[slug], v["id"]
        alvo = quando + dt.timedelta(seconds=60)
        espera = (alvo - agora()).total_seconds()
        if espera > 0 and not DRY:
            log(slug, vid, f"esperando {espera:.0f}s (sai {brt(quando)})")
            time.sleep(espera)
        ok_pub = v["status"]["privacyStatus"] == "public"
        limite = agora() + dt.timedelta(minutes=10)
        while not ok_pub and not DRY:
            s = yt.status(vid)
            if s is None:
                log(slug, vid, "vídeo sumiu (apagado?)"); break
            v = s
            ok_pub = s["status"]["privacyStatus"] == "public"
            if ok_pub or agora() > limite:
                break
            time.sleep(15)
        if not ok_pub and not DRY:
            if v and v["status"].get("publishAt") and iso(v["status"]["publishAt"]) > agora() + dt.timedelta(minutes=5):
                log(slug, vid, "reagendado para", v["status"]["publishAt"]); continue
            avisar(f"❌ {c['nome']}: https://youtu.be/{vid} não ficou público até T+10 min (tento de novo na próxima rodada)",
                   chave=f"naopub_{vid}", a_cada_h=6)
            continue
        try:
            fez = comentar_video(slug, c, yt, v, feitos)
        except Exception as e:
            log(slug, vid, "ERRO ao comentar:", e)
            avisar(f"❌ {c['nome']}: falhou comentar https://youtu.be/{vid}: {str(e)[:160]}", chave=f"errc_{vid}", a_cada_h=3)
            continue
        jsave(EST / "comentados.json", feitos)
        if fez and not DRY:
            fut = [q for q in futs.get(slug, []) if q > agora() + dt.timedelta(minutes=1)]
            prox_txt = f"Próximo vídeo: {brt(fut[0])}" if fut else "⚠️ Nenhum próximo vídeo agendado!"
            est_txt = f" · estoque até {brt(fut[-1])} ({len(fut)} agendados)" if fut else ""
            titulo = v["snippet"].get("title", "")[:70]
            avisar(f"💬 {c['nome']}: comentei em \"{titulo}\" https://youtu.be/{vid}\n{prox_txt}{est_txt}")
    # limpa registro antigo (> 30 dias)
    corte = agora() - dt.timedelta(days=30)
    feitos = {k: x for k, x in feitos.items() if iso(x["quando"]) > corte}
    jsave(EST / "comentados.json", feitos)


if __name__ == "__main__":
    main()
