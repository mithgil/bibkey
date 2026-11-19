# bibkey

引用 key 的查詢、驗證與去重，給人和 agent 用。單一 Python 檔（只用標準庫），
沒有相依套件。

```bash
bibkey --version          # bibkey 1.1
```

## 為什麼需要它

Zotero 的 Better BibTeX `citationKey` 存在資料庫裡，但**你的文稿編譯時讀的是
`.bib`**。兩者會不一致:

- 資料庫比 `.bib` 新（還沒匯出）→ 你從 Zotero 抄來的 key 在 `.bib` 裡不存在，
  貼進正文就是 `??`。
- 資料庫裡同一篇論文有多個 item → BBT 匯出時給每份加上 `a`/`b`/`c` 後綴；
  之後合併 item，後綴消失，之前抄下來的 key 就失效。

所以 `bibkey` 的**唯一真相是「這份文稿實際編譯的 `.bib`」**，不是資料庫。它會從
TeX root 的 `\addbibresource` / `\bibliography` 找出那個檔案（跟 LaTeX Workshop
同一套找法），再用它回答問題。

## 安裝

本體是一個 git repo，放在 `~/apps/bibkey`；`~/.local/bin/bibkey` 指過去。

```bash
git clone <repo> ~/apps/bibkey      # 或直接在 ~/apps/bibkey 開發
cd ~/apps/bibkey
./install.sh                        # chmod +x、建立 ~/.local/bin/bibkey symlink
```

`install.sh` 只做兩件事:確認執行權限、把 `~/.local/bin/bibkey` 指到這個 repo 的
`bibkey.py`。`~/.local/bin` 必須在 `PATH` 裡。

升級:

```bash
cd ~/apps/bibkey && git pull
```

因為是 symlink，`git pull` 之後直接生效，不用重裝。

單一檔案、只用標準庫，所以也可以複製到別的 repo（例如 `scripts/bibkey.py`），
或用 `python3 /path/to/bibkey.py` 執行。

## 快速開始

```bash
cd /path/to/manuscript
bibkey bibs                     # 這份文稿吃哪個 .bib
bibkey check                    # 正文引用但 .bib 沒有的 key、重複 key
bibkey find schottky barrier    # 用標題／作者／期刊的字找 key（詞序不限）
```

`check` 的輸出:

```
tex:  /…/SI/docs/main.tex
bibs: /…/SI/refs.bib
cited: 6 keys, in .bib: 507, uncited: 501
ok
```

## 找 TeX root 的規則

不給 `--tex` 時，從**目前目錄往下**找（最多 3 層）:

1. 檔案裡有 `\documentclass` 才算 root。
2. **有宣告 `.bib`（`\addbibresource` / `\bibliography`）的優先**，其次看修改時間最新。
3. 跳過 `archive/`、`build/`、`dist/`、`submission/`、`node_modules/` 與任何 `.`
   開頭的目錄（`.paperflow`、`.git` 等）。

所以這三個位置都會找到對的 root:

```bash
cd SI                 # → SI/docs/main.tex
cd SI/docs            # → SI/docs/main.tex
cd main/v2/tex        # → main/v2/tex/main.tex（natbib 那篇）
```

不確定它選了哪個，看 `check` / `dups` 印出來的 `tex:` 行，或明講:

```bash
bibkey check --tex docs/main.tex
bibkey check --tex ../main/v2/tex/main.tex
```

`--tex` 可以重複給，表示「這些文稿都算」——`merge` / `prune` 會取聯集（例如同一份
稿有兩個版本、或兩篇共用同一個 library）。

## 指令

| 指令 | 做什麼 | 寫入 |
|---|---|---|
| `bibs` | 印出在範圍內的 `.bib` | — |
| `find QUERY…` | 搜 `.bib`（key／標題／作者／期刊／type），印 `key⇥year⇥title` | — |
| `check` | 正文引用的 key 是否都在 `.bib`；重複 key | — |
| `zotero QUERY…` | 搜 `zotero.sqlite` 的 `citationKey`／標題，標記 `NOT IN .BIB` | — |
| `dedup` | `.bib` 裡同一個 key 寫了兩遍 | `.bib`（`--write`） |
| `prune` | `.bib` 裡有、但資料庫已不存在的條目 | `.bib`（`--write`） |
| `dups` | Zotero 裡同一篇論文的重複 items（唯讀） | 報表（`--write-report`） |
| `merge` | 把 `dups` 找到的重複 items 合併掉 | Zotero（`--yes`） |

共用參數:`--tex PATH`（可重複）、`--bib PATH`（可重複，覆蓋自動偵測）、
`--json`、`--limit N`（`find`/`zotero` 預設 10）。

### 離開碼

| 碼 | 意思 |
|---|---|
| 0 | ok |
| 1 | `find` / `zotero` 查無結果 |
| 2 | `check` 發現正文引用了 `.bib` 沒有的 key（**應該擋建置**）；或 `--strict` 下的重複 key；或 `merge` 有群組被跳過 |

重複 key 預設只印不擋——那是 library 端的衛生問題，BBT 下次匯出會再長回來，每次
都擋會讓人直接繞過閘門。

## `.bib` 清潔

```bash
bibkey dedup          # 預覽：哪些 key 被寫了兩遍
bibkey dedup --write
bibkey prune          # 預覽：哪些條目在資料庫裡已經不存在
bibkey prune --write
```

`prune` **不會刪掉正文正在引用的 key**。如果一個被引用的 key 在資料庫裡找不到，
它會保留並印 `KEPT … (cited, but no live Zotero item)`——那是要去看的問題，不是
要藏的。

## 合併 Zotero 重複 items

先看有沒有:

```bash
bibkey dups                                  # 印群組數與 KEEP 建議
bibkey dups --write-report ~/Zotero/duplicates.md
bibkey merge --plan                          # 只印計畫，不連 Zotero
bibkey merge                                 # dry run（問 Zotero，不寫）
bibkey merge --yes                           # 真的合併
bibkey prune --write                         # .bib 對齊資料庫
bibkey check
```

`merge` 需要 Zotero 正在跑，而且裝了 `zotero-dedupe` 插件 **1.2.0 以上**
（端點 `http://127.0.0.1:23119/dedupe/merge`）。寫入端點要求 token:

```bash
umask 077
python3 -c "import secrets;print(secrets.token_urlsafe(32))" \
    > ~/Zotero/zotero-dedupe-merge-token
```

合併走 Zotero 自己的 `mergeItems.mjs`:附件、筆記、標籤、collection 會搬到保留的
那一份，PDF 用內容 hash 去重，**被合併的副本進回收站**（清空回收站前可找回）。
欄位值不會被覆蓋，只補「保留那份缺的」——dry run 會列出每個要補的欄位。

KEEP 的選法:**正文有引用 > 沒有字母後綴 > 在 `.bib` 裡 > 字母順序**。所以合併
不會讓你正在引用的 key 消失。

真正的回滾是**關掉 Zotero 後的資料庫備份**（回收站只救得回 item，救不回已經
按 hash 併掉的附件）。

## 當編譯前的閘門

```bash
bibkey check --tex docs/main.tex || exit 1
latexmk -pdf docs/main.tex
```

`bibkey check` 的 exit 2 就是「這份 PDF 會有 `??`」。

## 給 agent 用

```bash
bibkey find "intense field emission" --json      # [{"key":…,"year":…,"title":…}]
bibkey check --json                              # {"tex":…,"missing":[…],"duplicates":[…]}
bibkey zotero crowell --json
bibkey dups --json                               # 群組 + 每項 in_bib / cited
```

## 常見問題

**`database is locked (5)`** — Zotero 開著時 `zotero.sqlite` 被鎖。`bibkey` 會自動
改讀一份 snapshot（含 `-wal`），所以查詢照常，只是可能落後幾秒。

**資料庫有、`.bib` 沒有** — 正常，代表還沒匯出。`bibkey zotero …` 會標
`NOT IN .BIB`。要讓它出現就重新匯出（Zotero → File → Export Library → Better
BibTeX）。

**`citationKey` 不在 `extra`** — BBT 把 key 存在自訂欄位（本機 fieldID 64），
`extra LIKE '%citation-key:%'` 永遠是 0 筆，別據此判斷 key 不存在。

**`.bib` 會不會被覆蓋** — 只有你設了 BBT 自動匯出才會。手動匯出的話，`dedup` /
`prune` 的結果會一直留著，直到你下次匯出。

## 檔案位置

```
~/apps/bibkey/bibkey.py             # 本體（單檔，只用標準庫），git repo 的內容
~/apps/bibkey/README.md             # 這份文件
~/.local/bin/bibkey                 # symlink → ~/apps/bibkey/bibkey.py
~/Zotero/zotero.sqlite              # 資料來源（唯讀）
~/Zotero/zotero-dedupe-merge-token  # merge 端點的 token（0600）
```

## 開發

```bash
cd ~/apps/bibkey
python3 -m py_compile bibkey.py     # 語法檢查
./bibkey.py find schottky --limit 3 # 直接跑（不需要安裝）
```

改完直接 commit 就生效（symlink 指向 repo 裡的工作檔）。單檔專案，沒有建置步驟。
