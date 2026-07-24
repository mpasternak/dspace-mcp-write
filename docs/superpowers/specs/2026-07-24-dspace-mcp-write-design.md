# dspace-mcp-write — serwer MCP z zapisem dla DSpace 7+

Data: 2026-07-24
Wersja: 1 (projekt zaakceptowany w brainstormingu, przed planem implementacji)
Status: do adwersaryjnego review (Fable ×2), potem plan implementacji

## Cel

Serwer MCP, dzięki któremu model językowy może **deponować i modyfikować** treść
w instancji DSpace 7+: tworzyć rekordy (workspaceitems), wgrywać do nich pliki
(bitstreamy), edytować metadane istniejących rekordów oraz zakładać kolekcje i
społeczności. Jest to uwierzytelniony nadzbiór serwera tylko-do-odczytu
[`dspace-mcp`](https://github.com/mpasternak/dspace-mcp): **wszystkie** narzędzia
odczytu tamtego pakietu są tu dostępne 1:1, plus warstwa zapisu.

Projekt jest generyczny — pakiet open source dla dowolnego użytkownika DSpace,
publikowany na GitHub (`mpasternak/dspace-mcp-write`) i PyPI
(`dspace-mcp-write`). Nie zawiera wiedzy o żadnym konkretnym systemie zewnętrznym
ani o żadnym konkretnym schemacie metadanych.

## Relacja do dspace-mcp

`dspace-mcp` (v0.1.1) świadomie przygotował grunt pod zapis (jego decyzja D7):
jedna prywatna metoda żądań w kliencie, `httpx.AsyncClient` w lifespanie (miejsce
na cookie jar rotującego CSRF), pola `username`/`password`/`enable_write` w
`Config` oraz warunkowa rejestracja narzędzi. `dspace-mcp-write` **korzysta z
`dspace-mcp` jako biblioteki** (zależność PyPI, pin `>=0.1.1,<0.2`) i dokłada:

- `WriteClient(DSpaceClient)` — subklasa z `login()`, `mutate()`, `upload()`;
- `auth.py` — handshake CSRF+JWT i rotacja tokenów;
- `patch.py` — tłumaczenie płaskiego słownika metadanych DC na JSON-Patch;
- narzędzia zapisu (deposit, upload, edycja metadanych, struktura);
- `server.py`, który rejestruje `dspace_mcp.server.READ_TOOLS` **oraz** narzędzia
  zapisu w jednym procesie.

Zero duplikacji kodu odczytu. Ryzyko sprzężenia z wewnętrznym API `dspace-mcp`
domykamy pinem wersji **i** testem importu (patrz „Testy").

## Zakres

### W zakresie

- Uwierzytelniony dostęp do jednej instancji DSpace (konto techniczne z
  konfiguracji).
- Deponowanie nowego rekordu w schemacie **draft-first**: utworzenie
  workspaceitem, wypełnienie metadanych, wgranie pliku. Publikacja (workflow /
  archiwizacja) jest **osobnym, jawnym** krokiem.
- Dorzucanie plików (bitstreamów) do istniejących, zarchiwizowanych rekordów.
- Edycja metadanych istniejących rekordów.
- Tworzenie kolekcji i społeczności.
- Pełny zestaw narzędzi odczytu z `dspace-mcp` (uwierzytelnionych — widzą też
  niepubliczne rekordy dostępne dla konta).
- Zabezpieczenia: biała lista kolekcji, podgląd/potwierdzenie (dry-run/confirm),
  wymóg poświadczeń do startu serwera.

### Poza zakresem

- Usuwanie zarchiwizowanych rekordów, bitstreamów i kolekcji/społeczności
  (`discard_workspace_item` sprząta **tylko** własne szkice — patrz niżej).
- Administracja użytkownikami, grupami, uprawnieniami ( resource policies).
- Zarządzanie procesem workflow (recenzja, akceptacja/odrzucenie cudzych
  workflowitems).
- OCR, generowanie miniatur, media filters (robi je DSpace po stronie serwera).
- Obsługa wielu instancji DSpace w jednym procesie (jak w `dspace-mcp`, D2).
- Legacy API DSpace 5/6 (`/rest`).
- Pobieranie pliku do wgrania z dowolnego URL (mniejsza powierzchnia SSRF) —
  wejście to lokalna ścieżka albo treść inline.

## Decyzje projektowe

### D1. Uruchomienie serwera = opt-in na zapis

Ten serwer istnieje po to, by pisać. Nie ma sensu robić z włączenia zapisu
osobnej ceremonii — kto nie chce zapisu, uruchamia `dspace-mcp` (read-only).
Efektywnym gate'em jest **wymóg poświadczeń**: bez `DSPACE_USERNAME` i
`DSPACE_PASSWORD` serwer nie wstaje i kończy czytelnym błędem konfiguracji.
Pole `enable_write` z `dspace-mcp.Config` **nie jest** tu używane jako
przełącznik.

### D2. Draft-first: publikacja jest osobnym, jawnym krokiem

DSpace ma trzy stany rekordu: workspace (szkic autora) → workflow (recenzja) →
archiwum (publiczny). Narzędzia tworzące i wgrywające plik zostawiają rekord w
stanie **workspace**. Dopiero `deposit_workspace_item` wypycha go dalej
(`POST /api/workflow/workflowitems`). To jedyne narzędzie, które publikuje, i —
jak każde mutujące — wymaga `confirm=true`. Dzięki temu model nie opublikuje
treści bez wyraźnego punktu kontrolnego.

Konsekwencja: to, czy `deposit_workspace_item` archiwizuje od razu, czy wrzuca
rekord do kolejki recenzji, zależy od konfiguracji **workflow danej kolekcji** —
narzędzie tego nie gwarantuje i mówi o tym w odpowiedzi (zwraca stan i typ
zwróconego obiektu: `workspaceitem` → `workflowitem` albo `item`).

### D3. Metadane jako płaski słownik DC, symetryczny do odczytu

Model podaje metadane w tym samym kształcie, w jakim `dspace-mcp` je zwraca przy
odczycie: mapa kluczy DC na wartości.

```json
{"dc.title": ["Tytuł pracy"],
 "dc.contributor.author": ["Kowalski, Jan", "Nowak, Anna"],
 "dc.date.issued": ["2026"]}
```

Wartością bywa lista stringów (owijamy każdy w `{"value": s}`) albo lista
obiektów `{"value", "language"?, "authority"?, "confidence"?}` dla przypadków z
autorytatywnymi wpisami. `patch.py` (czyste funkcje, zero I/O) tłumaczy słownik
na serię operacji JSON-Patch. Symetria z odczytem sprawia, że model może
przeczytać rekord, zmodyfikować słownik i odesłać go z powrotem.

### D4. Dwie różne ścieżki metadanych — nie mylić

- **Submission (workspaceitem):** JSON-Patch celuje w sekcje formularza:
  `path: /sections/<forma>/dc.title`. Nazwa sekcji jest konfigurowalna
  per-instancja (`submission-forms.xml`), więc **wykrywamy ją dynamicznie**
  (D5), nie hardkodujemy.
- **Archiwum (istniejący item):** JSON-Patch celuje w metadane obiektu:
  `path: /metadata/dc.title`. Schemat jest jednolity, bez sekcji. Prostsze.

### D5. Formularze submission wykrywamy, nie zakładamy

Głównym ryzykiem generyczności jest to, że wymagane pola i **nazwy sekcji**
formularza deponowania pochodzą z `submission-forms.xml` i różnią się między
instancjami (analogia do D8 w `dspace-mcp` dla filtrów). Dlatego:

- po `POST` tworzącym workspaceitem czytamy jego `sections` oraz
  `_embedded.submissiondefinition`; sekcję metadanych wybieramy jako tę, której
  komórka jest mapą metadanych DC (typ `submission-form`), a nie po nazwie;
- narzędzie diagnostyczne `get_submission_form(collection)` zwraca modelowi
  listę pól i informację, które są wymagane — **zanim** spróbuje deponować;
- gdy deposit nie powiedzie się z powodu braków, przekazujemy modelowi treść
  błędów walidacji z ciała odpowiedzi (D9), żeby wiedział, co uzupełnić.

### D6. Wejście pliku: lokalna ścieżka (albo treść inline)

Narzędzia MCP przyjmują argumenty JSON, więc surowych bajtów nie przekażemy
wygodnie. Podstawowe wejście to `file_path` — ścieżka na maszynie, gdzie działa
serwer (przy transporcie stdio to maszyna użytkownika; model odwołuje się do
pliku, który użytkownik wskazał). Alternatywa dla małych treści to
`content_base64`. Twardy limit rozmiaru: `DSPACE_UPLOAD_MAX_MB` (domyślnie 100).
Pobierania z URL nie ma (SSRF). Dokładnie jeden z `file_path`/`content_base64`
musi być podany; podanie obu albo żadnego → błąd walidacji argumentów.

### D7. Bezpieczeństwo warstwowe, ale bez przesady

Trzy warstwy, zgodnie z ustaleniami:

1. **Poświadczenia wymagane do startu** (D1) — bez konta nie ma serwera.
2. **Biała lista kolekcji** — `DSPACE_WRITE_COLLECTIONS` (lista UUID). Pusta =
   wszystkie kolekcje dozwolone (domyślnie). Narzędzia celujące w kolekcję
   (`create_workspace_item`, pośrednio deposit) i w item (dorzucanie pliku,
   edycja — przez kolekcję właścicielską) walidują cel **po naszej stronie**,
   zanim poleci żądanie; cel spoza listy → czytelny błąd bez HTTP.
3. **Dry-run / confirm** — każde narzędzie mutujące ma `confirm: bool = False`.
   Przy `False` (domyślnie) zwraca **podgląd** planowanej operacji (metoda,
   endpoint, cel, streszczenie metadanych, nazwa i rozmiar pliku) i **nie
   wykonuje** żadnego żądania mutującego. Model pokazuje plan użytkownikowi,
   dostaje zgodę i woła ponownie z `confirm=True`.

Świadomie **nie** wprowadzamy: podpisów, kolejki zatwierdzeń, RBAC po naszej
stronie — od autoryzacji jest DSpace i konto techniczne.

### D8. Rotacja tokenów przez event hooks httpx

CSRF i JWT w DSpace 7+ **rotują**. Zamiast wplatać obsługę tokenów w każde
wywołanie, rejestrujemy na współdzielonym `httpx.AsyncClient` dwa hooki:

- *request hook* dokłada `Authorization: Bearer <jwt>` i `X-XSRF-TOKEN` (z
  aktualnego cookie) do **każdego** żądania — także reużytych narzędzi odczytu;
- *response hook* podmienia zapisany JWT, gdy odpowiedź niesie świeży nagłówek
  `Authorization`; rotujący cookie `DSPACE-XSRF-TOKEN` aktualizuje sam cookie jar
  httpx.

Dzięki temu narzędzia odczytu z `dspace-mcp` działają bez zmian, a i tak są
uwierzytelnione.

### D9. Błędy zapisu przekazują treść walidacji z ciała

`dspace-mcp` świadomie **nie** przekazuje `message` z ciała błędu DSpace, bo przy
odczycie Spring wpisuje tam bezużyteczne „An exception has occurred". Przy
**zapisie** jest odwrotnie: DSpace zwraca przy 422/400 konkretną informację, w
której sekcji/polu jest problem. Dlatego warstwa zapisu ma **własne** mapowanie,
które dokleja zwięzłą treść błędu walidacji z ciała. Nowe/zmienione kody: 400
(walidacja argumentów), 403 („konto nie ma prawa zapisu tutaj" — inny komunikat
niż read-owe „niepubliczne"), 422 (błędy pól formularza), 409 (konflikt).

## Architektura

```
dspace-mcp-write/
├── pyproject.toml           # uv + hatchling; dep: dspace-mcp>=0.1.1,<0.2, httpx, mcp[cli]
├── README.md                # angielski, z badge'ami (readme-guardian)
├── LICENSE                  # MIT
├── .pre-commit-config.yaml  # ruff (jak w dspace-mcp)
├── .github/workflows/
│   ├── ci.yml               # matryca 3.10–3.13, ruff + pytest (unit; live off)
│   └── publish.yml          # trusted publishing (OIDC), środowisko `pypi`
├── src/dspace_mcp_write/
│   ├── __init__.py          # __version__
│   ├── config.py            # WriteConfig(Config): + write_collections, upload_max_mb
│   ├── auth.py              # handshake CSRF+JWT, hooki rotacji (I/O cienko)
│   ├── client.py            # WriteClient(DSpaceClient): login(), mutate(), upload()
│   ├── patch.py             # metadata dict → JSON-Patch (czyste funkcje)
│   ├── tools.py             # logika narzędzi zapisu (bez zależności od MCP)
│   └── server.py            # FastMCP: READ_TOOLS + WRITE_TOOLS, lifespan z login()
└── tests/
    ├── fixtures/            # realne odpowiedzi zapisu (login, workspaceitem, upload…)
    ├── conftest.py
    ├── test_auth.py
    ├── test_client.py
    ├── test_config.py
    ├── test_patch.py
    ├── test_tools.py
    ├── test_server.py
    └── test_live.py         # @pytest.mark.live — round-trip na demo.dspace.org
```

Granice odpowiedzialności (jak w `dspace-mcp`):

- `patch.py` — czyste funkcje, zero I/O. Najtańsze i najgęstsze testy.
- `auth.py` — handshake i konstrukcja hooków; I/O ograniczone do `login()`.
- `client.py` — `WriteClient`: metody mutujące i mapowanie błędów zapisu.
- `tools.py` — logika każdego narzędzia; przyjmuje `WriteClient`, nie wie o MCP.
- `server.py` — wyłącznie adapter MCP: lifespan (z logowaniem), rejestracja
  read+write, sprowadzenie wyjątków do `{"error": ...}`.

### Konfiguracja

| Zmienna | Domyślnie | Rola |
|---|---|---|
| `DSPACE_BASE_URL` | — (wymagane) | np. `https://demo.dspace.org/server` |
| `DSPACE_USERNAME` | — **(wymagane)** | konto techniczne (e-mail EPerson) |
| `DSPACE_PASSWORD` | — **(wymagane)** | hasło konta |
| `DSPACE_WRITE_COLLECTIONS` | pusta = wszystkie | biała lista UUID kolekcji (CSV) |
| `DSPACE_UPLOAD_MAX_MB` | `100` | twardy limit rozmiaru wgrywanego pliku |
| `DSPACE_TIMEOUT` | `15` | jak w `dspace-mcp` |
| `DSPACE_MAX_RESULTS` | `50` | jak w `dspace-mcp` (dla narzędzi odczytu) |

Każda nadpisywalna flagą CLI. `WriteConfig(Config)` dziedziczy po frozen
dataclass z `dspace-mcp` i dokłada `write_collections: tuple[str, ...]` oraz
`upload_max_mb: int`. Parsowanie env/CLI reużywa `normalize_base_url` z
`dspace-mcp`, a walidację poświadczeń dokłada (brak → `ValueError` z
komunikatem).

### Warstwa autoryzacji (`auth.py` + `WriteClient`)

Handshake wg kontraktu REST DSpace 7+ (do potwierdzenia na żywo na demo jako
pierwszy krok implementacji):

1. `GET /api/security/csrf` → serwer ustawia cookie `DSPACE-XSRF-TOKEN`
   (httpx trzyma cookie jar).
2. `POST /api/authn/login`, body form `user=<login>&password=<hasło>`, nagłówek
   `X-XSRF-TOKEN` z cookie. Sukces → JWT w **nagłówku odpowiedzi**
   `Authorization: Bearer …` (nie w ciele).
3. `GET /api/authn/status` — potwierdzenie `authenticated: true`.

`login()` woła się w lifespanie serwera przy starcie (fail-fast — złe konto ma
wywalić serwer od razu, nie w trakcie pierwszej operacji). Na `401` w trakcie
sesji (wygasły JWT) `mutate()`/`upload()` robią jednorazowe ponowne `login()` i
powtarzają żądanie; drugi `401` propaguje się jako błąd.

`WriteClient(DSpaceClient)`:

- `login()` — powyższy handshake; zapisuje JWT w instancji.
- `mutate(method, path, *, json=None, data=None, headers=None, where)` — żądanie
  mutujące (POST/PATCH/PUT/DELETE) pod ścieżkę względną wobec `/api`, z
  mapowaniem błędów zapisu (D9). JSON-Patch idzie jako `PATCH` z listą operacji.
- `upload(path, *, file_bytes, filename, fields=None, where)` — `POST`
  `multipart/form-data` (część `file` + opcjonalnie część `properties` typu
  `application/json`).
- reużywa `get()`, paginację i sondę z `DSpaceClient`.

## Narzędzia

Wszystkie narzędzia mutujące mają wspólny parametr `confirm: bool = False`
(D7.3). Poza tym:

| Narzędzie | Grupa | Endpoint(y) | Kluczowe parametry |
|---|---|---|---|
| `get_submission_form` | diagnostyka | `/api/config/submissiondefinitions/*`, `/api/core/collections/{uuid}` | `collection` |
| `create_workspace_item` | deposit | `POST /submission/workspaceitems?owningCollection=` + `PATCH` | `collection`, `metadata`, `confirm` |
| `upload_file_to_workspace_item` | deposit | `POST /submission/workspaceitems/{id}` (multipart) | `workspaceitem_id`, `file_path`/`content_base64`, `name?`, `confirm` |
| `deposit_workspace_item` | deposit | `POST /workflow/workflowitems` (`text/uri-list`) | `workspaceitem_id`, `confirm` |
| `discard_workspace_item` | deposit | `DELETE /submission/workspaceitems/{id}` | `workspaceitem_id`, `confirm` |
| `add_file_to_item` | item | `GET`/`POST /core/items/{uuid}/bundles`, `POST /core/bundles/{uuid}/bitstreams` | `item`, `file_path`/`content_base64`, `name?`, `confirm` |
| `update_item_metadata` | item | `PATCH /core/items/{uuid}` | `item`, `metadata`, `mode`, `confirm` |
| `create_collection` | struktura | `POST /core/collections?parent=` | `community`, `name`, `metadata?`, `confirm` |
| `create_community` | struktura | `POST /core/communities[?parent=]` | `name`, `parent?`, `metadata?`, `confirm` |

Plus **9 narzędzi odczytu** z `dspace-mcp` (`search_items`, `get_item`,
`list_communities`, `list_collections`, `list_bitstreams`, `get_bitstream_text`,
`list_facet_values`, `get_item_statistics`, `get_repository_info`) — rejestrowane
1:1 z `dspace_mcp.server.READ_TOOLS`.

### Szczegóły wybranych narzędzi

**`create_workspace_item`.** UUID kolekcji walidowany po naszej stronie i wobec
białej listy (D7.2). Tworzy workspaceitem (`owningCollection=<uuid>`), wykrywa
sekcję formularza (D5), buduje JSON-Patch z `metadata` (D3/D4) i patchuje.
Zwraca `workspaceitem_id`, wykryte sekcje, ewentualne błędy walidacji pól oraz
link UI. Rekord zostaje szkicem (D2). Przy `confirm=False` — sam podgląd.

**`upload_file_to_workspace_item`.** Czyta plik (D6), egzekwuje limit rozmiaru,
`POST` multipart do workspaceitem. Zwraca metadane utworzonego bitstreamu
(nazwa, `sizeBytes`, `checkSum`). Weryfikacja rozmiaru **przed** wczytaniem
całości do pamięci, gdy podano `file_path` (stat pliku); dla `content_base64` po
zdekodowaniu.

**`deposit_workspace_item`.** Jedyne narzędzie publikujące (D2). Body
`text/uri-list` = self-href workspaceitem. Zwraca typ i UUID powstałego obiektu
oraz jasny komunikat, czy rekord jest już zarchiwizowany, czy trafił do kolejki
workflow (zależnie od kolekcji).

**`add_file_to_item`.** Znajduje bundle `ORIGINAL` itemu; gdy go nie ma —
tworzy (`POST /core/items/{uuid}/bundles` z `{"name":"ORIGINAL"}`). Potem `POST`
multipart do `/core/bundles/{uuid}/bitstreams`. Walidacja przez kolekcję
właścicielską wobec białej listy.

**`update_item_metadata`.** `mode="merge"` (domyślnie) dokłada/zmienia podane
pola, nie ruszając reszty; `mode="replace"` zastępuje wartości podanych kluczy w
całości. JSON-Patch na `/metadata/...` (D4). Nie usuwa pól nieobecnych w
`metadata`.

**`get_submission_form`.** Zwraca pola formularza kolekcji i flagi „wymagane".
Model woła je **przed** deponowaniem, gdy nie zna wymagań instancji (D5).

### Kształt odpowiedzi

Narzędzia mutujące przy sukcesie zwracają zwięzły obiekt: co powstało/zmieniło
się (UUID/id, typ), link UI (gdy dotyczy) i ewentualne ostrzeżenia (np. błędy
walidacji pól, których model nie uzupełnił). Przy `confirm=False` zwracają
`{"preview": {...}, "confirm_required": true}` bez żadnego żądania mutującego.
Błędy — jak w `dspace-mcp` — jako `{"error": "..."}` (zdanie po angielsku), z
doklejoną treścią walidacji z ciała przy zapisie (D9).

## Obsługa błędów

Reużywamy `DSpaceError` i szkielet mapowania z `dspace-mcp`, ale warstwa zapisu
ma nadpisane mapowanie kodów mutujących (D9):

| Sytuacja | Komunikat |
|---|---|
| `400` | „the repository rejected the request" + treść walidacji z ciała |
| `401` (w trakcie) | ponowny `login()` i retry; drugi `401` → „authentication failed" |
| `403` | „the account is not allowed to write here (check its permissions on the target collection)" |
| `422` | „validation failed" + które sekcje/pola (z ciała) |
| `409` | „conflict" (z treścią) |
| `ConnectError`/`Timeout` | jak w `dspace-mcp` |
| przekroczony limit pliku | „file exceeds the N MB upload limit" |
| cel spoza białej listy | „collection <uuid> is not in the allowed write list" (bez HTTP) |

Zgodnie z globalną zasadą projektu: **żaden** blok `except` nie połyka błędu po
cichu — każdy loguje, przekształca albo zwraca sensowną odpowiedź dla modelu.

## Testy

`pytest` + `respx`, `asyncio_mode = "auto"` (jak w `dspace-mcp`).

**Jednostkowe (respx, domyślne):**

- `test_auth.py`: handshake CSRF→login→JWT (JWT z nagłówka odpowiedzi), rotacja
  (response hook podmienia JWT), re-login na 401 i retry, drugi 401 propaguje.
- `test_patch.py`: `metadata` dict → JSON-Patch dla ścieżki submission i dla
  ścieżki archiwum; stringi vs obiekty; `merge` vs `replace`. Czyste, najgęstsze.
- `test_client.py`: `mutate()` wysyła właściwą metodę i nagłówki; `upload()`
  buduje multipart; mapowanie błędów zapisu dokleja treść z ciała (D9).
- `test_tools.py`: każde narzędzie — sukces, `confirm=False` zwraca podgląd bez
  HTTP, odrzucenie spoza białej listy, błąd walidacji (422 z ciałem), limit
  pliku, wykrycie sekcji formularza (D5) na fixture.
- `test_config.py`: brak `USERNAME`/`PASSWORD` → `ValueError`; parsowanie białej
  listy i limitu; dziedziczenie po `Config`.
- `test_server.py`: rejestrują się read+write; **test importu** — API
  `dspace-mcp`, na którym stoimy (`DSpaceClient`, `READ_TOOLS`, sygnatury),
  istnieje; gdyby zniknęło, test pęka z jasnym komunikatem.

**Live (`@pytest.mark.live`, wyłączone domyślnie, `addopts = -m 'not live'`):**

Round-trip przeciwko `demo.dspace.org` z kontem podanym w env
(`DSPACE_TEST_USER`, `DSPACE_TEST_PASSWORD` — **nigdy w repo**, w CI z GitHub
Secrets):

1. `login()` → `authn/status` potwierdza zalogowanie.
2. `create_workspace_item` w kolekcji demo (UUID z env `DSPACE_TEST_COLLECTION`
   albo wykryty z `search`), metadane minimalne + wykryte wymagane.
3. `upload_file_to_workspace_item` z maleńkim PDF-em z `tests/fixtures/`.
4. Weryfikacja: workspaceitem ma bitstream i tytuł.
5. **Sprzątanie:** `discard_workspace_item` kasuje szkic (test nie zostawia
   śmieci na współdzielonym demo). Sprzątanie w `finally`, żeby leciało także po
   nieudanej asercji.

Live **nie** deponuje do archiwum na współdzielonym demo (zostawiałoby to trwały
rekord). Test „happy path" deposit jest opcjonalny i domyślnie pominięty.

**CI:** GitHub Actions, matryca Python 3.10–3.13, `ruff check` + `ruff format
--check` + `pytest` (tylko unit; live wymaga sekretów i jest osobnym, ręcznym /
crona jobem). Akcje spoza org `actions` przypięte do SHA (jak w `dspace-mcp`).

## Bezpieczeństwo i publikacja OSS

- **Sekrety:** poświadczenia testowe (publiczne konto demo DSpace) trzymamy
  **wyłącznie** w env/README testów jako „publiczne konto demo"; nigdy w kodzie
  ani w historii gita. Audyt `oss-github-publisher` przed publikacją.
- **Trusted publishing:** `publish.yml` przez OIDC (bez tokenu PyPI), środowisko
  `pypi`, `url: https://pypi.org/p/dspace-mcp-write`. Konfigurację po stronie
  PyPI (trusted publisher: repo `mpasternak/dspace-mcp-write`, workflow
  `publish.yml`, environment `pypi`) i GitHub (environment `pypi`) opisujemy
  użytkownikowi do ręcznego ustawienia.
- **Nazwa PyPI:** sprawdzić dostępność `dspace-mcp-write` przed publikacją.

## Kryteria ukończenia

1. `login()` działa przeciwko `demo.dspace.org` (test live) — handshake i
   rotacja potwierdzone empirycznie.
2. Round-trip live: create → upload → weryfikacja → discard (ze sprzątaniem).
3. Wszystkie narzędzia mutujące respektują `confirm` (dry-run bez HTTP) i białą
   listę.
4. Testy jednostkowe (respx) przechodzą na matrycy 3.10–3.13; test importu
   chroni sprzężenie z `dspace-mcp`.
5. Narzędzia odczytu z `dspace-mcp` działają w tym serwerze bez zmian
   (uwierzytelnione).
6. README (badge'y, instalacja, konfiguracja klienta MCP, tabela narzędzi,
   sekcja bezpieczeństwa) oraz `publish.yml` z trusted publishing.
7. Audyt `oss-github-publisher` bez blokerów; repo publiczne
   `mpasternak/dspace-mcp-write`.

## Otwarte punkty do potwierdzenia na żywo (pierwszy krok implementacji)

Zanim rozpiszemy narzędzia, potwierdzamy na `demo.dspace.org` (bo od tego zależy
kształt kodu, a wiedza pochodzi z kontraktu REST, nie z żywej próby):

- dokładny handshake CSRF+JWT i miejsce JWT (nagłówek `Authorization`);
- kształt odpowiedzi `POST /submission/workspaceitems` i nazwa sekcji formularza
  na demo (weryfikacja mechanizmu wykrywania z D5);
- format `POST` multipart dla bitstreamu (część `file`, część `properties`);
- czy `deposit` na demo archiwizuje od razu, czy idzie w workflow;
- `DELETE` workspaceitem zwraca `204` i faktycznie sprząta.
