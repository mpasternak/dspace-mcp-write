# dspace-mcp-write — serwer MCP z zapisem dla DSpace 7+

Data: 2026-07-24
Wersja: 2 (po adwersaryjnym review Fable #1 i **weryfikacji live** na demo.dspace.org, DSpace 10.1-SNAPSHOT)
Status: do drugiego review Fable, potem plan implementacji

## Cel

Serwer MCP, dzięki któremu model językowy może **deponować i modyfikować** treść
w instancji DSpace 7+: tworzyć rekordy (workspaceitems), wgrywać do nich pliki
(bitstreamy), edytować metadane istniejących rekordów oraz zakładać kolekcje i
społeczności. Jest to uwierzytelniony nadzbiór serwera tylko-do-odczytu
[`dspace-mcp`](https://github.com/mpasternak/dspace-mcp): **wszystkie** narzędzia
odczytu tamtego pakietu są tu dostępne 1:1, plus warstwa zapisu.

Projekt jest generyczny — pakiet open source dla dowolnego użytkownika DSpace,
publikowany na GitHub (`mpasternak/dspace-mcp-write`) i PyPI
(`dspace-mcp-write`; nazwa wolna — zweryfikowane). Nie zawiera wiedzy o żadnym
konkretnym systemie zewnętrznym ani o żadnym konkretnym schemacie metadanych.

## Weryfikacja empiryczna (demo.dspace.org, 2026-07-24)

Handshake i flow deposit **zweryfikowano na żywo** kontem admina na
`demo.dspace.org` (DSpace 10.1-SNAPSHOT). Potwierdzone: login CSRF+JWT,
utworzenie workspaceitem, wykrywanie sekcji formularza, PATCH metadanych, upload
pliku (201), `DELETE` szkicu (204). Szczegóły i dokładne nazwy nagłówków —
w treści (D5, D8). Niezweryfikowane live (niższe ryzyko, potwierdzone wobec
`RestContract`, sprawdzane w testach live): deposit do workflow, dorzucanie
bitstreamu do istniejącego itemu, PATCH `/core/items`, tworzenie
kolekcji/społeczności — świadomie pominięte, by nie zostawiać artefaktów na
współdzielonym demo.

## Relacja do dspace-mcp

`dspace-mcp` (v0.1.1, **jest na PyPI**) świadomie przygotował grunt pod zapis
(jego decyzja D7). `dspace-mcp-write` **korzysta z niego jako biblioteki**
(zależność PyPI, pin `>=0.1.1,<0.2`) i dokłada `WriteClient(DSpaceClient)`,
`auth.py`, `patch.py`, narzędzia zapisu oraz `server.py` rejestrujący
`dspace_mcp.server.READ_TOOLS` **oraz** narzędzia zapisu w jednym procesie.

**Realny zakres reużycia** (zweryfikowany wobec źródeł `dspace-mcp` 0.1.1):

- **Reużywalne wprost:** klasa `DSpaceClient` (subklasowalna — jeden chokepoint
  `_request_json`, nadpisywalny `_error_for_status`, domyślny cookie jar httpx),
  `DSpaceError`, `shaping.*`, `dspace_mcp.server.READ_TOOLS` (9 funkcji
  owiniętych `_guard`, biorących `Context` i pobierających klienta z
  `lifespan_context.client` zwykłym dostępem do atrybutu — działa z dowolnym
  obiektem lifespanu mającym atrybut `client`), oraz `normalize_base_url`.
- **NIE reużywalne wprost:** `config_from_env` i `parse_args` zwracają twardo
  `Config` (nie subklasę), nie czytają `username`/`password`, a `_build_parser`
  jest prywatny z `prog="dspace-mcp"`. Warstwę env/CLI **reimplementujemy**,
  reużywając tylko `normalize_base_url` i samą dataclass `Config`.
- **Uwaga o kliencie HTTP:** `DSpaceClient.build_http()` nie przyjmuje
  `event_hooks` ani `**kwargs`. `WriteClient` nadpisuje `build_http()`: woła
  `super().build_http()`, po czym ustawia `http.event_hooks = {...}` (to
  ustawialna właściwość `httpx.AsyncClient`) i własny `User-Agent`.
- **Uwaga o zależności (do zgłoszenia w dspace-mcp, poza tym repo):**
  `dspace_mcp/__init__.py` deklaruje `0.1.0`, a `pyproject.toml` `0.1.1` —
  drobny rozjazd wersji; nasz `User-Agent` i tak nadpisujemy.

Ryzyko sprzężenia z wewnętrznym API domykamy pinem wersji **i** testem importu,
który sprawdza obecność `DSpaceClient`, `READ_TOOLS`, `normalize_base_url`,
`build_http` i pęka jasnym komunikatem, gdy tamten pakiet zmieni API.

## Zakres

### W zakresie

- Uwierzytelniony dostęp do jednej instancji DSpace (konto techniczne z
  konfiguracji).
- Deponowanie nowego rekordu **draft-first**: utworzenie workspaceitem,
  wypełnienie metadanych, wgranie pliku, **udzielenie licencji**, naprawa
  metadanych szkicu. Publikacja (workflow/archiwizacja) jest osobnym, jawnym
  krokiem.
- Dorzucanie plików (bitstreamów) do istniejących, zarchiwizowanych rekordów.
- Edycja metadanych istniejących rekordów.
- Tworzenie kolekcji i społeczności (z zastrzeżeniem o białej liście — D7.2).
- Pełny zestaw narzędzi odczytu z `dspace-mcp` (uwierzytelnionych).
- Zabezpieczenia: biała lista kolekcji, podgląd/potwierdzenie (dry-run/confirm),
  wymóg poświadczeń do startu serwera.

### Poza zakresem

- Usuwanie zarchiwizowanych rekordów, bitstreamów, kolekcji/społeczności
  (`discard_workspace_item` kasuje **tylko** własne szkice).
- Administracja użytkownikami, grupami, uprawnieniami (resource policies).
- Zarządzanie cudzym procesem workflow (akceptacja/odrzucenie workflowitems).
- OCR, generowanie miniatur, media filters (robi je DSpace serwerowo).
- Wiele instancji DSpace w jednym procesie (jak `dspace-mcp`, D2).
- Legacy API DSpace 5/6 (`/rest`).
- Pobieranie wgrywanego pliku z dowolnego URL (mniejsza powierzchnia SSRF).

## Decyzje projektowe

### D1. Uruchomienie serwera = opt-in na zapis

Ten serwer istnieje po to, by pisać. Efektywnym gate'em jest **wymóg
poświadczeń**: bez `DSPACE_USERNAME` i `DSPACE_PASSWORD` serwer nie wstaje i
kończy czytelnym błędem konfiguracji. Odziedziczone po `Config` pole
`enable_write` **jest ignorowane** (nie jest tu przełącznikiem).

### D2. Draft-first: publikacja jest osobnym, jawnym krokiem

DSpace ma trzy stany rekordu: workspace (szkic) → workflow (recenzja) → archiwum
(publiczny). Narzędzia tworzące/wgrywające zostawiają rekord w stanie
**workspace**. Dopiero `deposit_workspace_item` (`POST /api/workflow/workflowitems`)
wypycha go dalej i — jak każde mutujące — wymaga `confirm=true`.

**Kompletny deposit wymaga trzech rzeczy** (potwierdzone kontraktem; brak
którejkolwiek → 422 przy deposicie):

1. metadanych wymaganych przez formularz kolekcji (na demo: `dc.title`,
   `dc.date.issued`, `dc.type` — wykrywane z `get_submission_form`, D5);
2. co najmniej jednego pliku (sekcja `upload`), jeśli formularz go wymaga;
3. **udzielonej licencji deponowania** — sekcja `license`, JSON-Patch
   `add /sections/license/granted = "true"`.

Dlatego licencji **nie da się pominąć**. Obsługujemy ją jawnym parametrem
`grant_license: bool = False` na `deposit_workspace_item` (patrz Narzędzia),
udokumentowanym jako **prawnie znaczący** (oznacza akceptację licencji
repozytorium w imieniu konta). Przy `grant_license=False` deposit nie próbuje
publikować, tylko zwraca informację, że licencja jest wymagana.

`deposit_workspace_item` po `POST` robi **jeden GET** powstałego obiektu, by
rzetelnie zaraportować stan końcowy: rekord zarchiwizowany od razu
(`inArchive: true`) czy w kolejce workflow — zależnie od konfiguracji workflow
kolekcji. Narzędzie tego nie gwarantuje, tylko mówi, co się stało.

### D3. Metadane jako płaski słownik DC, symetryczny do odczytu

Model podaje metadane w kształcie, w jakim `dspace-mcp` je zwraca przy odczycie:

```json
{"dc.title": ["Tytuł pracy"],
 "dc.contributor.author": ["Kowalski, Jan", "Nowak, Anna"],
 "dc.date.issued": ["2026"], "dc.type": ["Article"]}
```

Wartość: lista stringów (owijamy w `{"value": s}`) albo lista obiektów
`{"value","language"?,"authority"?,"confidence"?}`. `patch.py` (czyste funkcje,
zero I/O) tłumaczy słownik na JSON-Patch.

### D4. Dwie różne ścieżki metadanych — nie mylić

- **Submission (workspaceitem):** JSON-Patch celuje w sekcje formularza:
  `/sections/<sekcja>/dc.title`. Nazwy sekcji zależą od kolekcji (D5).
  `Content-Type: application/json`. Operacja `add` ustawia wartość pola.
- **Archiwum (istniejący item):** JSON-Patch celuje w metadane obiektu:
  `/metadata/dc.title`. By zbudować poprawny patch (`add` gdy pola brak vs
  `replace` gdy jest), potrzebny jest **bieżący stan metadanych** — dlatego
  `update_item_metadata` najpierw GET-uje item. `patch.py` pozostaje czysty:
  przyjmuje `(bieżące_metadane, żądane)` i zwraca operacje.

### D5. Formularze submission wykrywamy z konfiguracji (nie z zawartości)

Głównym ryzykiem generyczności jest to, że **nazwy sekcji** i wymagane pola
pochodzą z `submission-forms.xml`/`item-submission.xml` i różnią się między
kolekcjami. **Zweryfikowane na demo:** ta sama instancja ma sekcje
`traditionalpageone` (definicja `traditional`) **albo** `publicationStep` +
`traditionalpagetwo` (definicja `Publication`) — zależnie od kolekcji. Sztywne
`traditionalpageone` daje **422**.

Heurystyka „sekcja będąca mapą DC" **NIE działa**: na świeżym workspaceitem
sekcje-formularze serializują się jako **puste obiekty** (zweryfikowane). Zamiast
tego:

1. Sekcje bierzemy z odpowiedzi `POST` tworzącej workspaceitem (klucze
   `sections`). (Definicja submission bywa nieembedowana — nie zakładamy embedu;
   w razie potrzeby GET po definicję.)
2. **Które sekcje są formularzami i jakie pola hostują** — ustalamy pytając
   `GET /api/config/submissionforms/{id-sekcji}` dla każdej sekcji:
   - **200** → sekcja-formularz; ciało `rows[].fields[].selectableMetadata[0].metadata`
     daje listę pól DC, a `fields[].mandatory` flagi „wymagane"
     (zweryfikowane: na demo `publicationStep` → `dc.title` mandatory,
     `dc.date.issued` mandatory, `dc.type` mandatory, `dc.contributor.author`
     opcjonalne, itd.);
   - **400** → sekcja nie-formularzowa (`license`, `upload`, `collection`).
3. **Routing pól:** każdy klucz z `metadata` kierujemy do **pierwszej**
   sekcji-formularza, która go deklaruje. Domyślne definicje mają **wiele**
   sekcji metadanych (np. `page1`: tytuł/autor/data; `page2`: temat/abstrakt) —
   „ta sekcja" (l. poj.) było błędem. Klucz, którego nie deklaruje żadna sekcja,
   → czytelny błąd (nie ślepy 422).
4. `get_submission_form(collection)` zwraca modelowi pola i flagi „wymagane"
   **zanim** deponuje. `create_workspace_item` używa tego samego mechanizmu
   wewnętrznie.

### D6. Wejście pliku: lokalna ścieżka (albo treść inline)

Podstawowe wejście: `file_path` — ścieżka na maszynie serwera (stdio → maszyna
użytkownika). Alternatywa dla małych treści: `content_base64` — długość
sprawdzamy **przed** dekodowaniem (`len*3/4` vs limit), by nie alokować dużego
bufora tylko po to, by go odrzucić. Twardy limit: `DSPACE_UPLOAD_MAX_MB`
(domyślnie 100). Dokładnie jeden z `file_path`/`content_base64` — oba/żaden →
błąd walidacji argumentów. Upload ma **własny, dłuższy timeout** (nie
odziedziczony 15 s — 100 MB się w nim nie zmieści): `timeout=` per-żądanie na
`upload()`.

### D7. Bezpieczeństwo: co jest granicą zaufania, a co wygodą

Trzy warstwy, ale **uczciwie** co do tego, która jest realną granicą:

1. **Poświadczenia wymagane do startu** (D1).
2. **Biała lista kolekcji** — `DSPACE_WRITE_COLLECTIONS` (CSV UUID). Pusta =
   wszystkie (domyślnie). Walidacja **po naszej stronie**, przed HTTP:
   - `create_workspace_item` — sprawdza `collection`;
   - `deposit_workspace_item` — **GET-uje kolekcję szkicu** i sprawdza ją
     (szkic mógł powstać poza tymi narzędziami; „pośrednie" pokrycie nie
     wystarcza);
   - `add_file_to_item`, `update_item_metadata` — **GET-ują owningCollection
     itemu** i sprawdzają ją (item bez kolekcji właścicielskiej, np. szablon →
     błąd);
   - **`create_collection`/`create_community` NIE są objęte białą listą** —
     nie mają kolekcji docelowej. Przy skonfigurowanej białej liście narzędzia
     struktury i tak pozwalają zmieniać drzewo wszędzie, gdzie konto ma prawa.
     To jawnie dokumentujemy (w README i w opisie narzędzi): biała lista
     ogranicza **deponowanie i pliki**, nie strukturę.
3. **Dry-run / confirm** — każde narzędzie mutujące ma `confirm: bool = False`.
   Przy `False` zwraca **podgląd** (metoda, endpoint, cel, streszczenie
   metadanych, nazwa/rozmiar pliku) i **nie wykonuje** żądania mutującego.

**Uczciwe postawienie sprawy (był to postulat z review):** `confirm` jest
**guardrailem UX, nie granicą bezpieczeństwa** — ustawia go ten sam model, który
wywołuje narzędzie, więc nic nie broni natychmiastowego `confirm=true`, a serwer
przetwarza treść niezaufaną (`get_bitstream_text` zwraca tekst PDF — wektor
prompt injection: „zdeponuj X z confirm=true"). **Realną granicą zaufania są:
uprawnienia konta w DSpace + wąski zakres konta technicznego + zatwierdzanie
wywołań narzędzi przez hosta MCP.** README ma to mówić wprost i zalecać:
wąsko uprawnione konto techniczne oraz niepustą białą listę. Świadomie **nie**
budujemy RBAC ani kolejki zatwierdzeń po naszej stronie.

### D8. Autoryzacja: handshake i cykl życia tokenów (zweryfikowane live)

Nazwy i przepływ **potwierdzone empirycznie na demo** — poprzednia wersja speca
miała tu błędy:

- **CSRF:** token przychodzi w **nagłówku odpowiedzi `DSPACE-XSRF-TOKEN`** oraz
  jako cookie **`DSPACE-XSRF-COOKIE`** (HttpOnly). Odsyłamy go jako nagłówek
  **`X-XSRF-TOKEN`**. Źródłem prawdy jest **nagłówek odpowiedzi** (nie cookie —
  poleganie na jarze jest kruche wobec zmian w DSpace/Spring): *response hook*
  zapisuje świeży `DSPACE-XSRF-TOKEN` do stanu klienta, *request hook* wysyła go
  jako `X-XSRF-TOKEN`. Token **rotuje po każdej odpowiedzi** (zweryfikowane).
- **Pozyskanie tokenu:** `GET /api/security/csrf` (istnieje **od 7.5**). Na 404
  (7.0–7.4) fallback na tani GET, np. `GET /api/authn/status`, który też zwraca
  nagłówek z tokenem. Kompatybilność deklarujemy jako **7.5+ pewna, 7.0–7.4
  przez fallback**.
- **Login:** `POST /api/authn/login`, body form `user=<login>&password=<hasło>`,
  nagłówek `X-XSRF-TOKEN`. Sukces → JWT w **nagłówku odpowiedzi
  `Authorization: Bearer …`** (zweryfikowane).
- **Uwaga o CT create:** `POST /submission/workspaceitems` **wymaga**
  `Content-Type: application/json` nawet przy pustym ciele — inaczej **415**
  (zweryfikowane).
- **Cykl życia JWT:** JWT **nie rotuje** per-odpowiedź — nowy `Authorization`
  przychodzi tylko z login/refresh; w normalnym ruchu token po prostu **wygasa**
  (domyślnie ~30 min). Dlatego samo „podmienianie JWT z odpowiedzi" nie
  wystarcza — potrzebny jest re-login (niżej).
- **Doklejanie nagłówków tylko do hosta DSpace (KRYTYCZNE):** *request hook*
  dokleja `Authorization` i `X-XSRF-TOKEN` **wyłącznie**, gdy host żądania ==
  host `base_url`. Powód (zweryfikowany w źródłach httpx 0.28): przy
  `follow_redirects=True` request-hooki odpalają się na **każdym hopie**
  przekierowania, już po tym, jak httpx zdejmuje `Authorization` dla hopów
  cross-origin. Bitstreamy (`/content`) i `pid/find` przekierowują 302 często do
  S3/CDN — bez tego warunku doklejalibyśmy **uprzywilejowany JWT do obcego
  hosta** (wyciek do logów S3/CDN) i **psuli pobieranie** (presigned URL odrzuca
  drugi mechanizm auth). To także chroni reużyte `get_bitstream_text`/`stream_bytes`.
- **Rejestracja hooków:** `WriteClient.build_http()` → `super().build_http()`,
  potem `http.event_hooks = {"request":[_inject], "response":[_capture_csrf]}` i
  własny `User-Agent` (`dspace-mcp-write/<wersja>`).

`login()` woła się w lifespanie serwera przy starcie (fail-fast).

### D9. Błędy zapisu: przekazujemy walidację, rozróżniamy 401/403

`dspace-mcp` świadomie **nie** przekazuje `message` z ciała (przy odczycie Spring
wpisuje tam bezużyteczne „An exception has occurred"). Przy **zapisie** DSpace
zwraca konkretne błędy pól — więc warstwa zapisu ma własne mapowanie.

- **401 w trakcie sesji (wygasły JWT):** re-login **raz** i powtórzenie żądania;
  drugi 401 → „authentication failed". Ta logika musi objąć **także ścieżkę
  odczytu** — reużyte narzędzia idą przez `DSpaceClient.get()` → `_request_json()`,
  które nie ma retry; bez tego po ~30 min każdy odczyt cicho degraduje i zwraca
  **odziedziczony** komunikat „this server queries DSpace anonymously" (nieprawdę
  w serwerze uwierzytelnionym). Dlatego `WriteClient` **nadpisuje
  `_request_json()`** (dokłada 401→relogin→retry) **oraz `_error_for_status()`**
  (komunikaty 401/403 właściwe dla serwera z kontem).
- **403 na żądaniu mutującym:** może oznaczać **nieświeży token CSRF** (DSpace
  zwraca wtedy 403 z nowym tokenem w nagłówku), a nie brak uprawnień. `mutate()`/
  `upload()` na 403: odświeżają token z odpowiedzi i **powtarzają raz**; dopiero
  drugi 403 → „the account is not allowed to write here (check its permissions on
  the target collection)".

| Sytuacja | Komunikat |
|---|---|
| `400` | „the repository rejected the request" + treść walidacji z ciała |
| `401` (po relogin) | „authentication failed" |
| `403` (po odświeżeniu CSRF i retry) | „…not allowed to write here…" |
| `422` | „validation failed" + które sekcje/pola (z ciała) |
| `409` | „conflict" (z treścią) |
| `415` | „missing/unsupported Content-Type" (nie powinno wystąpić — ustawiamy sami) |
| limit pliku | „file exceeds the N MB upload limit" |
| cel spoza białej listy | „collection <uuid> is not in the allowed write list" (bez HTTP) |
| klucz metadanych bez sekcji | „no submission form section accepts <klucz>" (bez HTTP) |

Zgodnie z zasadą projektu: **żaden** `except` nie połyka błędu po cichu.

## Architektura

```
dspace-mcp-write/
├── pyproject.toml           # uv + hatchling; dep: dspace-mcp>=0.1.1,<0.2, httpx, mcp[cli]
├── README.md                # angielski, badge'y, sekcja bezpieczeństwa (readme-guardian)
├── LICENSE                  # MIT
├── .pre-commit-config.yaml  # ruff (jak w dspace-mcp)
├── .github/workflows/
│   ├── ci.yml               # matryca 3.10–3.13, ruff + pytest (unit; live off)
│   └── publish.yml          # trusted publishing (OIDC), środowisko `pypi`
├── src/dspace_mcp_write/
│   ├── __init__.py          # __version__
│   ├── config.py            # WriteConfig(Config) frozen: + write_collections, upload_max_mb
│   ├── auth.py              # handshake CSRF+JWT, hooki (origin-scoped inject, capture CSRF)
│   ├── client.py            # WriteClient(DSpaceClient): login(), mutate(), upload(),
│   │                        #   nadpisane _request_json()/_error_for_status(), build_http()
│   ├── patch.py             # (bieżące, żądane) → JSON-Patch (czyste funkcje)
│   ├── forms.py             # wykrywanie sekcji i routing pól (D5) — czyste + cienki GET
│   ├── tools.py             # logika narzędzi zapisu (bez zależności od MCP)
│   └── server.py            # FastMCP: READ_TOOLS + WRITE_TOOLS, lifespan z login()
└── tests/
    ├── fixtures/            # realne odpowiedzi (login, csrf, workspaceitem, forms, upload…)
    ├── conftest.py
    ├── test_auth.py         # handshake, rotacja CSRF, origin-scoping hooka
    ├── test_client.py       # mutate/upload, 401-relogin (read+write), 403-CSRF retry
    ├── test_config.py
    ├── test_patch.py
    ├── test_forms.py        # detekcja sekcji i routing pól (D5)
    ├── test_tools.py
    ├── test_server.py       # rejestracja read+write, test importu API dspace-mcp
    └── test_live.py         # @pytest.mark.live — round-trip na demo (non-required)
```

Granice: `patch.py`/`forms.py` — czyste (routing pól jako czysta funkcja od
listy sekcji-formularzy; jedyne I/O to `GET submissionforms`). `auth.py` — hooki
i handshake. `client.py` — `WriteClient`. `tools.py` — logika narzędzi (bez MCP).
`server.py` — adapter MCP.

### Konfiguracja

| Zmienna | Domyślnie | Rola |
|---|---|---|
| `DSPACE_BASE_URL` | — (wymagane) | np. `https://demo.dspace.org/server` |
| `DSPACE_USERNAME` | — **(wymagane)** | konto techniczne (e-mail EPerson) |
| `DSPACE_PASSWORD` | — **(wymagane)** | hasło konta |
| `DSPACE_WRITE_COLLECTIONS` | pusta = wszystkie | biała lista UUID kolekcji (CSV) |
| `DSPACE_UPLOAD_MAX_MB` | `100` | twardy limit rozmiaru wgrywanego pliku |
| `DSPACE_TIMEOUT` | `15` | jak w `dspace-mcp` (upload ma własny, dłuższy) |
| `DSPACE_MAX_RESULTS` | `50` | jak w `dspace-mcp` (dla narzędzi odczytu) |

Każda nadpisywalna flagą CLI. `WriteConfig(Config)` — **`@dataclass(frozen=True)`**
(inaczej `TypeError`), dziedziczy po `Config`, dokłada
`write_collections: tuple[str, ...]` i `upload_max_mb: int`. Warstwę env/CLI
reimplementujemy (reużywając `normalize_base_url`); brak `USERNAME`/`PASSWORD`
→ `ValueError` z komunikatem. `enable_write` z `Config` ignorujemy.

### Warstwa autoryzacji (`auth.py` + `WriteClient`)

`WriteClient(DSpaceClient)`:

- `login()` — handshake z D8; zapisuje JWT w instancji, token CSRF w stanie.
- `mutate(method, path, *, json=None, data=None, headers=None, where)` — żądanie
  mutujące pod ścieżkę względną wobec `/api`; 401→relogin→retry, 403→odśwież
  CSRF→retry (D9); mapowanie błędów zapisu z ciałem.
- `upload(path, *, file_bytes, filename, fields=None, where, timeout=…)` — `POST`
  multipart (część `file` + opcjonalnie `properties` typu `application/json`),
  własny dłuższy timeout.
- nadpisane `_request_json()` (401→relogin dla ścieżki odczytu) i
  `_error_for_status()` (401/403 dla serwera z kontem).
- nadpisane `build_http()` (event_hooks + własny User-Agent).

## Narzędzia

Wszystkie mutujące mają `confirm: bool = False` (D7.3 — guardrail UX).

| Narzędzie | Grupa | Endpoint(y) | Kluczowe parametry |
|---|---|---|---|
| `get_submission_form` | diagnostyka | `GET /config/submissionforms/*` (+ sekcje z ws) | `collection` |
| `create_workspace_item` | deposit | `POST /submission/workspaceitems?owningCollection=` + `PATCH` | `collection`, `metadata`, `confirm` |
| `update_workspace_item_metadata` | deposit | `PATCH /submission/workspaceitems/{id}` | `workspaceitem_id`, `metadata`, `confirm` |
| `upload_file_to_workspace_item` | deposit | `POST /submission/workspaceitems/{id}` (multipart) | `workspaceitem_id`, `file_path`/`content_base64`, `name?`, `confirm` |
| `deposit_workspace_item` | deposit | GET kolekcji szkicu → `PATCH .../license/granted` → `POST /workflow/workflowitems` → GET stanu | `workspaceitem_id`, `grant_license`, `confirm` |
| `discard_workspace_item` | deposit | `DELETE /submission/workspaceitems/{id}` | `workspaceitem_id`, `confirm` |
| `add_file_to_item` | item | GET owningCollection → GET/POST `/core/items/{uuid}/bundles` → `POST /core/bundles/{uuid}/bitstreams` | `item`, `file_path`/`content_base64`, `name?`, `confirm` |
| `update_item_metadata` | item | GET item → `PATCH /core/items/{uuid}` | `item`, `metadata`, `confirm` |
| `create_collection` | struktura | `POST /core/collections?parent=` | `community`, `name`, `metadata?`, `confirm` |
| `create_community` | struktura | `POST /core/communities[?parent=]` | `name`, `parent?`, `metadata?`, `confirm` |

Plus **9 narzędzi odczytu** z `dspace-mcp`, rejestrowanych 1:1.

### Szczegóły wybranych narzędzi

**`create_workspace_item`.** Waliduje UUID kolekcji (nasza strona + biała lista).
Tworzy workspaceitem (`Content-Type: application/json`), wykrywa sekcje i routing
pól (D5), buduje JSON-Patch, patchuje. **Semantyka porażki cząstkowej:** gdy POST
się uda, a PATCH zwróci 422 — narzędzie **zwraca `workspaceitem_id` + błędy pól i
zostawia szkic** (nie kasuje), by model mógł go naprawić `update_workspace_item_metadata`.
Przy `confirm=False` — sam podgląd.

**`update_workspace_item_metadata`.** Naprawa/uzupełnienie metadanych istniejącego
szkicu (ta sama maszyneria detekcji sekcji i patcha co create). Pozwala wyjść z
sytuacji „draft istnieje, ale walidacja pól przeszła tylko częściowo".

**`deposit_workspace_item`.** Jedyne publikujące (D2). GET-uje kolekcję szkicu
(biała lista). Gdy `grant_license=True` — najpierw `PATCH /sections/license/granted`.
Potem `POST /workflow/workflowitems` (`text/uri-list` = self-href). Na końcu GET
powstałego obiektu → raport, czy zarchiwizowany, czy w workflow. Gdy
`grant_license=False` — nie publikuje; zwraca, że licencja jest wymagana.

**`update_item_metadata`.** GET-uje bieżące metadane, buduje patch (`add` gdy
pola brak, `replace` gdy jest). Semantyka: **dla każdego podanego klucza ustawia
całą listę wartości tego klucza** (dodaje, gdy brak); klucze niepodane bez zmian;
pusta lista dla klucza = usunięcie pola. Brak osobnego `mode` (jedna, jasna
semantyka). `patch.py` czysty: `(bieżące, żądane) → operacje`.

**`add_file_to_item`.** GET owningCollection (biała lista) → znajduje/tworzy
bundle `ORIGINAL` → `POST /core/bundles/{uuid}/bitstreams` (multipart `file` +
opcjonalnie `properties`).

**`get_submission_form`.** Pola formularza kolekcji + flagi „wymagane". Model
woła je **przed** deponowaniem.

### Kształt odpowiedzi

Sukces: zwięzły obiekt (UUID/id, typ, link UI, ostrzeżenia — np. niespełnione
pola wymagane). `confirm=False`: `{"preview": {...}, "confirm_required": true}`
bez żądania mutującego. Błędy: `{"error": "..."}` (angielski) z treścią walidacji
przy zapisie (D9).

## Testy

`pytest` + `respx`, `asyncio_mode = "auto"`.

**Jednostkowe (respx, domyślne):**

- `test_auth.py`: handshake CSRF→login→JWT (JWT z nagłówka odpowiedzi; nazwy
  `DSPACE-XSRF-TOKEN`/`DSPACE-XSRF-COOKIE`/`X-XSRF-TOKEN`); rotacja CSRF z
  nagłówka; **origin-scoping** — żądanie przekierowane na inny host **nie** niesie
  `Authorization` (regresja z review #1); fallback `security/csrf` 404 → status.
- `test_patch.py`: `(bieżące, żądane)` → JSON-Patch dla submission i archiwum;
  `add` vs `replace`; usuwanie pustą listą; stringi vs obiekty. Najgęstsze.
- `test_forms.py`: detekcja sekcji (200/400 z `submissionforms/{id}`), routing
  pól do właściwej sekcji, wiele sekcji metadanych, klucz bez sekcji → błąd.
- `test_client.py`: `mutate()`/`upload()` — metoda, nagłówki, multipart;
  **401→relogin→retry na ścieżce read** (przez `_request_json`) i write; **403→
  odśwież CSRF→retry**; drugi 401/403 propaguje; mapowanie błędów z ciałem;
  nadpisane `_error_for_status` daje komunikaty uwierzytelnione.
- `test_tools.py`: każde narzędzie — sukces, `confirm=False` podgląd bez HTTP,
  odrzucenie spoza białej listy, 422 z ciałem, limit pliku, licencja przy
  deposicie, porażka cząstkowa create (id+błędy, szkic zostaje).
- `test_config.py`: brak `USERNAME`/`PASSWORD` → `ValueError`; parsowanie białej
  listy/limitu; `WriteConfig` frozen; `enable_write` ignorowane.
- `test_server.py`: rejestrują się read+write; **test importu** API `dspace-mcp`
  (`DSpaceClient`, `READ_TOOLS`, `normalize_base_url`, `build_http`).

**Live (`@pytest.mark.live`, wyłączone domyślnie, `addopts = -m 'not live'`):**

Round-trip przeciw `demo.dspace.org`, konto z env (`DSPACE_TEST_USER`,
`DSPACE_TEST_PASSWORD` — **nigdy w repo**; w CI z GitHub Secrets):
login → `create_workspace_item` → `upload_file_to_workspace_item` (mały PDF z
fixtures) → weryfikacja → **`discard_workspace_item` w `finally`** (id
surfaced także przy porażce, by sprzątanie zadziałało). Osobny, opcjonalny test
asertuje **kształt 422** deposit bez licencji (bez publikowania na demo).

**Uwaga o demo (z review):** `demo.dspace.org` bywa resetowane co tydzień, bywa
wolne/niedostępne, a konto jest publiczne (współbieżny szum). Dlatego job live
jest **non-required**, asercje **luźne** (istnienie, nie liczniki), a kryteria
wydania **nie** zależą twardo od live. Docelowo rozważyć lokalny DSpace w
docker-compose jako pewniejszą weryfikację przedwydaniową.

**CI:** GitHub Actions, matryca 3.10–3.13, `ruff check` + `ruff format --check` +
`pytest` (unit). Live wymaga sekretów i jest osobnym, ręcznym/crona jobem
(non-required). Akcje spoza org `actions` przypięte do SHA (jak w `dspace-mcp`).

## Bezpieczeństwo i publikacja OSS

- **Sekrety:** poświadczenia testowe (publiczne konto demo) tylko w env/README
  testów jako „publiczne konto demo"; nigdy w kodzie ani w historii gita. Audyt
  `oss-github-publisher` przed publikacją.
- **Trusted publishing:** `publish.yml` przez OIDC (bez tokenu PyPI), środowisko
  `pypi`, `url: https://pypi.org/p/dspace-mcp-write`. Konfigurację po stronie
  PyPI (trusted publisher: repo `mpasternak/dspace-mcp-write`, workflow
  `publish.yml`, environment `pypi`) i GitHub (environment `pypi`) opisujemy
  użytkownikowi do ręcznego ustawienia.
- **README (sekcja bezpieczeństwa):** wprost, że `confirm` to guardrail UX;
  realną granicą są uprawnienia konta + wąski zakres + zatwierdzanie przez hosta
  MCP; zalecenie wąskiego konta i niepustej białej listy.

## Kryteria ukończenia

1. `login()` działa przeciw `demo.dspace.org` (test live) — handshake, rotacja
   CSRF, origin-scoping potwierdzone. *(non-required — patrz uwaga o demo)*
2. Round-trip live: create → upload → weryfikacja → discard (sprzątanie w
   `finally`).
3. Wszystkie mutujące respektują `confirm` (dry-run bez HTTP) i białą listę
   (z jawnym wyjątkiem narzędzi struktury).
4. Testy jednostkowe (respx) przechodzą na 3.10–3.13; test importu chroni
   sprzężenie; test origin-scopingu i 401/403-retry przechodzą.
5. Narzędzia odczytu z `dspace-mcp` działają uwierzytelnione, z retry na 401 i
   właściwymi komunikatami błędów.
6. Deposit z licencją i naprawą metadanych szkicu działa end-to-end (przynajmniej
   test kształtu 422 bez licencji + zielony happy-path w środowisku kontrolowanym).
7. README (badge'y, instalacja, konfiguracja klienta MCP, tabela narzędzi, sekcja
   bezpieczeństwa) + `publish.yml` z trusted publishing.
8. Audyt `oss-github-publisher` bez blokerów; repo publiczne
   `mpasternak/dspace-mcp-write`.
