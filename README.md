# yt-comentarios

Comentário do dono do canal ~1 min depois que cada vídeo/Short sai, em todos os canais T0LD.
Roda só no GitHub Actions (`.github/workflows/comentar.yml`, a cada 10 min) — nada no PC.

- Vídeos: YouTube Data API (OAuth de cada canal no segredo `YT_TOKENS`). A API não fixa comentário.
- Texto: Claude Haiku (`ANTHROPIC_API_KEY`); links obrigatórios conferidos no código; Achadinhos é montado sem IA.
- `canais.json`: idioma, estilo, exemplos e links por canal.
- `estado/`: `comentados.json`, `status.json` (próximo vídeo / estoque por canal), `avisos.jsonl` (o PC lê e manda no WhatsApp).
- Aviso de lacuna: canal com menos de 24 h de vídeos agendados no YouTube (repete a cada 12 h).
