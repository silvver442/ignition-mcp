# Aktualizacja WebDev Bridge do 0.1.2

`tag_config_get` wywołuje `tags/scoped-state`. W 0.1.1 dla istniejącego tagu
`_scoped_tag_snapshot` pobiera konfigurację przez `system.tag.getConfiguration`,
przekazuje jej element bezpośrednio do `system.util.jsonEncode`, a potem dekoduje
wynik. Przy tworzeniu `tags/scoped-configure` wywołuje ten sam odczyt po otrzymaniu
`Good` z `system.tag.configure`. Błąd odczytu po tym punkcie może pozostawić
utworzony tag. Odpowiedź `ok: false` z tekstem `Java StackOverflowError` powstaje
w obsłudze wyjątku handlera, przed kodowaniem odpowiedzi WebDev.
Próba na Designerze 8.3.9 dla czterech tagów Cuppera potwierdziła, że pobranie
konfiguracji zwraca listę, a `Java StackOverflowError` pojawia się przy
`system.util.jsonEncode(configs[0])`. Natywny eksport zwrócił tekst `unicode`,
który dał poprawny słownik i pozwolił zakodować kopertę odpowiedzi jako JSON.

W 0.1.2 eksporter Ignition generuje JSON dla pojedynczego tagu. Wywołanie
`system.tag.exportTags(tagPaths=[path], recursive=False, exportType='json')`
zwraca tekst w pamięci. Mostek sprawdza, czy wynik zawiera dokładnie jeden
pasujący tag atomowy, i zachowuje wszystkie jego pola. `effective` pozostaje
odczytem właściwości w dobrej jakości, w tym `defaultValue`, `historyEnabled`
i `historyProvider`. Wartości domyślne mogą być pominięte w samym eksporcie.
[Eksport tagów w Ignition 8.3](https://www.docs.inductiveautomation.com/docs/8.3/appendix/scripting-functions/system-tag/system-tag-exportTags)
[Format JSON tagów w Ignition 8.3](https://docs.inductiveautomation.com/docs/8.3/platform/scripting/json-format)

## Diagnostyka przed aktywacją

- Po ponownym połączeniu klienta z konfiguracją `.codex/config.toml` użyj
  `tag_browse` dla `Line_1/Cupper/Ignition` w providerze `default` oraz
  `tag_read` dla `[default]Line_1/Cupper/Ignition/Outfeed_Counter`.
- Launcher z dostępem do poświadczeń MCP może wykonać podpisane odczyty poleceniem
  `./.local/start-ignition-mcp.sh --diagnose-scoped-tags`.
- `bridge/probe_tag_serialization.py` uruchomiony w Script Console sprawdza osobno
  pobranie konfiguracji, stary enkoder i dekoder, natywny eksport, kodowanie
  koperty JSON oraz jakość odczytu właściwości. Natychmiast po błędzie wypisuje
  etap i stos. Odpowiedź HTTP sprawdzają podpisane operacje WebDev.

## Jednorazowa aktywacja

Eksport MCP z testowego Gateway potwierdził zgodność biblioteki 0.1.1 z poprawką
oraz zgodność `doPost` z lokalnym delegatem. Zweryfikowana kopia z diagnostyki
znajduje się w `backups/mcp-bridge/bridge-0.1.1-prepared-6c3b70d25f2f4a4aba88f4cddfb9c03a/`.

Po decyzji o wdrożeniu uruchom z katalogu `ignition-scada` w środowisku,
w którym działa dotychczasowy launcher MCP:

```sh
./.local/start-ignition-mcp.sh --upgrade-bridge-012 --apply
```

Skrypt sprawdza Gateway 8.3.9 i aktywny mostek 0.1.1. Porównuje eksport
projektu z podpisanym odczytem biblioteki, po czym zapisuje kopię wyłącznie
zmienianego pliku w `backups/mcp-bridge/bridge-0.1.1-…/`. Kopia i manifest
powstają przed zapisem. Oryginalne bajty są odtwarzane testowo w pamięci z
istniejącym `BRIDGE_SECRET`, a w kopii zapisany jest jawny znacznik zastępczy.
Manifest umożliwia zweryfikowanie treści kopii i wyniku jej rekonstrukcji
przed ewentualnym użyciem przez administratora.

Skrypt zapisuje wyłącznie `mcp-bridge/ignition/script-python/mcp_bridge_lib/code.py`,
odczytuje plik po zapisie, wykonuje skan projektów i potwierdza aktywną wersję
oraz zestaw tras. Następnie przez cztery wywołania `tags/scoped-state` weryfikuje
odczyt konfiguracji i raportuje źródła wartości oraz ustawienia historii.
Dla `Outfeed_Counter` sprawdza typ `Memory`, `Int4`, początkowe `0`, wyłączoną
historię i bieżące `0` o jakości `Good`. Zapis tagów pozostaje ograniczony
do `[default]Line_1/Cupper/Ignition` przez istniejące reguły mostka i klienta.
[Skan projektów w Ignition 8.3](https://docs.inductiveautomation.com/docs/8.3/tutorials/version-control-guide)

Raport `uncertain` wskazuje ostatni etap oraz kopię. Sprawdź `bridge_status`
i `tag_config_get` przed decyzją o kolejnym działaniu. Pełną konfigurację każdego
tagu zwraca `tag_config_get`.
