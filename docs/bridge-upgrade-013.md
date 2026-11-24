# Przygotowanie tagów Cuppera przez Bridge 0.1.3

Mostek 0.1.2 obsługuje tworzenie tagów i zmianę właściwości historii.
Wersja 0.1.3 dodaje dwie wewnętrzne trasy z dokładnym zakresem. Akcja
`tag_configure_scoped(action="prepare_cupper")` obsługuje tylko `Speed`
i `Production` pod `[default]Line_1/Cupper/Ignition`. Oryginalne trasy
i reguły zapisu pozostają takie same. Ogólne narzędzia tworzenia, importu
i zapisu konfiguracji tagów pozostają poza profilem MCP.

## Historian i przywracanie

Dokumentacja SQL Historian 8.3 przypisuje Integer i Long do tej samej kategorii
`sqlth_te.datatype = 0` oraz kolumny `intvalue`. Zmiana typu może wycofać
dotychczasowy wpis metadanych i utworzyć nowy. Dotychczasowe próbki są związane
z identyfikatorem starego wpisu. Migracja operuje tylko na konfiguracji
istniejącego taga. [Tabela SQL Historian w Ignition 8.3](https://docs.inductiveautomation.com/docs/8.3/appendix/reference-pages/ignition-database-table-reference#sqlth_te)

Dla `Production` osobny odczyt `tag_cupper_production_history` pobiera liczbę
i SHA-256 dla surowych próbek z `Postgres_Historian`. Domyślne zamknięte okno to
siedem dni kończących się pięć minut przed zapytaniem. Skrót obejmuje znaczniki
czasu z milisekundami, wartości oraz jakości. Migracja wymaga niepustego
odczytu i sprawdza to samo okno na Gateway bezpośrednio przed zmianą, a klient
sprawdza je ponownie po niezależnym odczycie taga. Jest to weryfikacja
reprezentatywnego okna, a nie całego archiwum ani fizycznej szerokości kolumn
istniejącej bazy. [Ignition 8.3 `queryRawPoints`](https://docs.inductiveautomation.com/docs/8.3/appendix/scripting-functions/system-historian/system-historian-queryRawPoints)

`Speed` przechodzi z Expression na Memory przy zachowaniu Int4 i bieżącego
wyniku wyrażenia jako wartości początkowej. `Production` przechodzi z Int4 na
Int8 przy zachowaniu bieżącej wartości i dotychczasowego `defaultValue`. Zapis
używa `MergeOverwrite`, a klient porównuje pełną zapisaną konfigurację poza
polami przeznaczonymi do zmiany oraz efektywne ustawienia historii.
[Ignition 8.3 `system.tag.configure`](https://docs.inductiveautomation.com/docs/8.3/appendix/scripting-functions/system-tag/system-tag-configure)

Każdy zapis poprzedza zweryfikowana kopia w `backups/mcp-tag-config/`.
`tag_config_restore` korzysta z podpisanego potwierdzenia wyniku. Przywrócenie
`Speed` odtwarza oryginalne wyrażenie, a `Production` wraca do Int4 wyłącznie
przy zgodnej wartości w zakresie Int4 i zgodnych próbkach. Jeśli licznik
lub konfiguracja zmieniły się od migracji, narzędzie wstrzymuje przywrócenie.
`exclusive_access=true` potwierdza, że inni autorzy konfiguracji i źródła zapisu
tych tagów pozostają wstrzymani. Ignition nie oferuje dla tej operacji zapisu
warunkowego względem bieżącej wartości taga.

## Aktywacja mostka i migracja

Z katalogu `ignition-scada` uruchom w środowisku dotychczasowego launchera MCP:

```sh
./.local/start-ignition-mcp.sh --upgrade-bridge-013 --apply
```

Skrypt pobiera aktualny eksport projektu mostka, zapisuje i sprawdza kopię
biblioteki bez poświadczenia w pliku, zachowuje `BRIDGE_SECRET`, zapisuje
wyłącznie bibliotekę mostka i potwierdza odczyt czterech tagów oraz dostęp do
odczytu historii. Wdrożenie samo nie migruje tagów.

1. Po ponownym połączeniu klienta MCP potwierdź `bridge_status` 0.1.3 i wykonaj
   `tag_cupper_production_history`. Przed migracją wymagany jest dodatni `count`.
2. Odczytaj świeży `state` dla każdego taga przez `tag_config_get`.
3. Wywołaj `tag_configure_scoped` osobno dla `Speed`, a następnie dla
   `Production`, z `action="prepare_cupper"`, własnym `expected_state`,
   `exclusive_access=true` i `confirm=true`. Zachowaj dowody kopii. Jeśli
   weryfikacja zwróci `uncertain`, odczytaj aktualny stan i historię przed
   ponowieniem lub decyzją o ręcznym odzyskaniu.
4. Potwierdź, że oba tagi nadal mają historię w `Postgres_Historian`, licznik
   zachował wartość i ta sama ścieżka działa na wykresie Perspective.

## Gateway Timer Script bez Designera

Na tym Gateway `Test_Gemma_v1` jest aktywnym projektem Perspective. Najmniejszy
zakres zmian daje osobny projekt symulatora bez dziedziczenia. Gateway Timer
Script jest zasobem projektu. Przygotuj jego format z działającego eksportu
Ignition 8.3.9, z `Enabled=false` i `Fixed Delay`. Wybierz osobny wątek, jeśli
wykonanie może blokować wspólny wątek zegarów. Aktualizacja skryptów restartuje
zdarzenia w ich projekcie. [Gateway Event Scripts w Ignition 8.3](https://docs.inductiveautomation.com/docs/8.3/platform/scripting/scripting-in-ignition/gateway-event-scripts#timer-script)

Profil MCP ma kontrolowany deploy widoków Perspective. Dla timera dodaj osobną
operację przyjmującą wyłącznie zweryfikowany pakiet jednego projektu i jednego
zasobu. Operacja powinna wykonać kopię poprzedniego stanu, przypiąć wersję,
zastosować podpisany zapis i skan projektów, a potem porównać eksport z Gateway
oraz stan w diagnostyce Gateway. Timer ma używać dokładnych ścieżek tagów
Cuppera, sprawdzać typ i jakość i kończyć cykl przy błędzie odczytu. Włącz timer
osobną kontrolowaną operacją po zweryfikowaniu migracji i zasobu. Ten etap
nie wymaga zmian projektu Perspective ani konfiguracji dostawcy historii.
[Skan projektów przez API w Ignition 8.3](https://docs.inductiveautomation.com/docs/8.3/tutorials/version-control-guide#pulling-changes-installation-b)
