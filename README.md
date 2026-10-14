# Geminiprox — Gemini 4 Argon Web Proxy

> Pakai **Gemini 4 Argon** dari `gemini.google.com` lewat API OpenAI-compatible + 9Router. Web UI buat login, unlimited akun, auto fallback kalau limit habis.

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

---

## Apa ini?

Proxy yang mengubah **Gemini Web** (`gemini.google.com` → endpoint `BardChatUi/data/assistant.lamda.BardFrontendService/StreamGenerate`) jadi **OpenAI-compatible API**.

- **Model utama:** `gemini-4-argon` — Tugas kompleks (muncul di Gemini App sebagai `4 Argon`)
- **Model lain:** `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.1-pro`, `gemini-auto`, `gemini-flash-lite`
- **Web UI:** login sekali paste cookie, auto ambil `SNlM0e` + `BL`, test reply
- **Multi-akun:** unlimited akun Google, sistem **try & fallback** (akun 1 error/limit → otomatis coba akun 2, 3, dst dalam 1 request)
- **Kompatibel:** OpenAI `/v1/chat/completions`, streaming SSE, tools/function calling, Google native `/v1beta/models`

Repo ini juga menyertakan **Antigravity legacy proxy** (`antigravity_proxy.py`) untuk Cloud Code Assist API — tapi untuk Argon pakai `argon_web_proxy.py`.

---

## Demo

```
Hermes / OpenAI SDK  →  http://127.0.0.1:8787/v1  →  gemini.google.com  →  balik
                      →  http://127.0.0.1:20128/v1 (via 9Router, prefix argon/)
```

- Direct: `gemini-4-argon`
- Via 9Router: `argon/gemini-4-argon` (9Router paksa prefix untuk custom provider)

---

## Quick Start

### 1. Clone & jalanin

```bash
git clone https://github.com/RajaskaJanuardani/geminiprox.git
cd geminiprox

# tanpa dependency wajib (httpx opsional buat streaming beneran)
python argon_web_proxy.py

# atau custom port
python argon_web_proxy.py --port 8787
```

Buka **http://127.0.0.1:8787/**

### 2. Login (30 detik)

1. Klik **Buka Gemini** di web UI → login Google kalau belum (pastikan akun Pro yang sudah dapat `4 Argon`)
2. Di tab Gemini tekan **F12** → tab **Application** (Chrome) / **Storage** (Firefox) → **Cookies** → `https://gemini.google.com`
3. Copy nilai `__Secure-1PSID` dan `__Secure-1PSIDTS` — atau klik kanan → **Copy all** sebagai cookie string:
   ```
   __Secure-1PSID=g.a00...; __Secure-1PSIDTS=sidts-...; ...
   ```
4. Paste di web UI → **Simpan & Test** → kalau `✓ Login OK — Argon siap` berarti berhasil. Proxy auto ambil `SNlM0e` + `BL`.

> Tambah akun lagi? Klik **+ Tambah Akun** di panel — isi nama + cookie akun ke-2, ke-3, dst. Unlimited.

### 3. Pakai sebagai OpenAI API

**Tanpa 9Router (direct):**

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-4-argon","messages":[{"role":"user","content":"halo"}]}'
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8787/v1", api_key="anything")
res = client.chat.completions.create(model="gemini-4-argon", messages=[{"role":"user","content":"halo"}])
print(res.choices[0].message.content)
```

**Via 9Router:**

1. 9Router → **Providers** → **Add Provider** → **OpenAI Compatible**
2. **Base URL:** `http://127.0.0.1:8787/v1`
3. **Prefix:** `argon`
4. Save → model muncul sebagai `argon/gemini-4-argon`, `argon/gemini-3.6-flash`, dll

```bash
# via 9Router (contoh key 9Router)
curl http://127.0.0.1:20128/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-9router-xxx" \
  -d '{"model":"argon/gemini-4-argon","messages":[{"role":"user","content":"halo"}]}'
```

**Hermes config:**

```yaml
model:
  provider: custom
  base_url: http://127.0.0.1:20128/v1   # atau http://127.0.0.1:8787/v1 kalau tanpa 9Router
  api_key: sk-9router-xxx
  default: argon/gemini-4-argon        # atau gemini-4-argon kalau direct
```

---

## Multi-akun & Fallback

- Web UI → panel **Akun Google** → list semua akun (`Akun 1 ✓`, `Akun 2 ✓`, ...)
- Tiap akun ada switch **on/off**, tombol **▶ Test**, **✕ Hapus**
- **Sistem:** request coba akun **1 → 2 → 3 …** otomatis. Kalau akun 1 kena `429`, `BardErrorInfo`, atau timeout, langsung pindah ke akun berikutnya **dalam 1 request** — klien gak perlu retry.
- Cocok buat Argon: tidak semua akun Pro dapat Argon, jadi campur akun Argon + Flash biar gak bottleneck.

File akun disimpan di `accounts.json` (auto migrate dari `cookie.txt` lama). Jangan commit file ini.

```
accounts.json  ← gitignored
cookie.txt     ← gitignored (legacy)
argon_config.json ← BL + SNlM0e cache
```

---

## API

| Method | Path | Deskripsi |
|--------|------|-----------|
| `GET` | `/` | Web UI login |
| `GET` | `/api/accounts` | List akun (`accounts`, `enabled_count`, `total`) |
| `POST` | `/api/accounts` | Tambah akun `{name, cookie}` |
| `POST` | `/api/accounts/{id}/toggle` | On/off akun |
| `POST` | `/api/accounts/{id}/test` | Test akun (refresh SNlM0e/BL + `hi`) |
| `DELETE` | `/api/accounts/{id}` | Hapus akun |
| `GET` | `/v1/models` | List model OpenAI |
| `GET` | `/v1beta/models` | List model Google native |
| `POST` | `/v1/chat/completions` | Chat (stream & tools) |
| `POST` | `/v1beta/models/{model}:generateContent` | Google native |

Model: `gemini-4-argon`, `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-thinking`, `gemini-3.1-pro`, `gemini-auto`, `gemini-flash-lite`
Sufiks `@think=N` tersedia: `gemini-3.5-flash@think=0`

---

## File

```
argon_web_proxy.py      # Main — Gemini web → OpenAI (Argon)
antigravity_proxy.py    # Legacy — Cloud Code Assist (OAuth, bukan web)
README.md
LICENSE
.gitignore
```

`argon_web_proxy.py` pure Python stdlib + `httpx` opsional (buat streaming beneran, kalau gak ada fallback ke `urllib` chunked).

---

## Troubleshooting

- **BardErrorInfo [2] / 403** → cookie/SNlM0e/BL expired. Re-login: ambil cookie baru → Simpan & Test. Atau klik **▶ Test** per-akun.
- **429 RESOURCE_EXHAUSTED** → limit akun habis. Proxy auto fallback ke akun berikutnya kalau ada multi-akun. Tambah akun lagi atau tunggu reset.
- **Model `argon/...` not found di 9Router** → cek Base URL `http://127.0.0.1:8787/v1` dan Prefix `argon`. Restart 9Router, cek `/v1/models` harus ada `argon/gemini-4-argon`.
- **Port 8787 kepakai** → `python argon_web_proxy.py --port 8790` lalu update Base URL di 9Router.

---

## Disclaimer

Proyek ini reverse-engineer endpoint web Gemini (`StreamGenerate`). Gunakan dengan akun sendiri, patuhi ToS Google. Tidak ada spoofing tier — request tetap pakai kuota akun lu. Untuk akses Argon resmi, butuh Google AI Pro/Ultra yang sudah dapat rollout Argon.

---

## License

MIT — bebas pakai, modifikasi, jual. Jangan lupa kasih star ⭐ kalau berguna.

## Credits

- Base web2api logic terinspirasi dari `alex23243/gemini-web2api` & `Sophomoresty/gemini-web2api`
- Antigravity proxy dari `usamashehab/antigravity-proxy`
