# Próba migracji źródła Cupper

Próba używa wyłącznie `[default]Line_1/Cupper/Ignition/MCP_Test_SourceMigration`. Wymaga istniejącej listy dozwolonych folderów z `[default]Line_1/Cupper/Ignition`, potwierdzenia zapisu i wyłącznego dostępu do tego jednego tagu. Ścieżka, typ, wyrażenie i polityki kolizji są ustalone w kodzie.

## Odczyt Bridge 0.1.5

W konfiguracji nowej sesji MCP udostępnij `bridge_scoped_result_verify` z lokalnego forka. Narzędzie działa z aktywną wersją 0.1.5. Wykonuje podpisane `files/read` pliku `mcp-bridge/ignition/script-python/mcp_bridge_lib/code.py` i porównuje drzewo składniowe `_scoped_result` z lokalnym źródłem. Wynik to wersja, zgodność wersji z `ping`, zgodność funkcji i jej skróty. Odczyt jest przetwarzany w pamięci.

## Udostępnienie próby

Po potwierdzeniu zgodności funkcji i po osobnej zgodzie na aktualizację użyj przygotowanego `bridge/apply_upgrade_016.py --apply` w skonfigurowanym środowisku klienta. Procedura najpierw sprawdza zgodność podpisanego odczytu z eksportem w pamięci i zachowuje odtwarzalną kopię z usuniętym sekretem. Następnie dodaje jedną trasę do Bridge i weryfikuje przez podpisany odczyt i `ping`, czy trasa działa. W nowej sesji MCP udostępnij `tag_source_migration_test`, pozostawiając ogólne zapisy i ogólny odczyt plików poza listą narzędzi sesji.

## Aktualizacja do Bridge 0.1.7

Użyj istniejącego launchera. Aktualizator porównuje obecną bibliotekę 0.1.6 z wersją używaną przy wdrożeniu. Przed zapisem odtwarza bajt w bajt kopię z usuniętym `BRIDGE_SECRET`, korzystając ze skonfigurowanego sekretu. Ścieżka i skróty SHA-256 są w raporcie i w pliku `manifest.json` obok kopii. Po skanowaniu projektów porównuje podpisany odczyt i niezależny eksport projektu z przygotowaną biblioteką. Podpisany `ping` musi potwierdzić `runtime_version` równą `0.1.7` i ten sam zestaw tras.

```sh
/Users/pietia/Development/ignition-scada/.local/start-ignition-mcp.sh --upgrade-bridge-017 --apply
```

Po aktualizacji sprawdź `status=success` i `active_library_verified=true`. W nowej sesji MCP potwierdź `0.1.7` przez `bridge_status`. Dopiero wtedy wykonaj pojedyncze, osobno uzgodnione `action=create` na tagu testowym. Pole `configure.diagnostic` może zawierać identyfikator rozpoznanej kategorii lub `redacted`. Bridge odczytuje maksymalnie 1024 znaki do klasyfikacji i zwraca wyłącznie identyfikator.

## Aktualizacja do Bridge 0.1.8

```sh
/Users/pietia/Development/ignition-scada/.local/start-ignition-mcp.sh --upgrade-bridge-018 --apply
```

Aktualizator stosuje procedurę kopii i weryfikacji opisaną dla 0.1.7. W `merge` i `overwrite` zmienia wyłącznie format argumentu `tags` na listę słowników. Po wdrożeniu potwierdź `runtime_version=0.1.8` i `active_library_verified=true`. Istniejący tag Expression nadaje się do osobno uzgodnionej próby `merge` z pokwitowaniem jego utworzenia.

`configuration_unchanged=true` oznacza, że odczyt konfiguracji zgadza się z kopią. `status=uncertain` po `overwrite` z wynikiem innym niż `Good` oznacza, że klient nie potwierdził wykonania operacji. `rejected` i pokwitowanie pozwalające rozważyć Overwrite są zarezerwowane dla sprawdzonego odrzucenia `merge`.

## Wykonanie próby

1. Odczytaj stan przez `tag_config_get`. Dla nieobecnego tagu użyj `tag_source_migration_test` z `action=create`, `expected_state` z odczytu, `confirm=true` i `exclusive_access=true`. Zweryfikuj `AtomicTag`, `Expression`, `Int4`, wyłączoną historię i jakość `Good`. Jeśli tag już istnieje, odczytaj jego stan i zakończ próbę bez zapisu.
2. Po osobnej decyzji o teście migracji uruchom raz `action=merge` z aktualnym stanem i polami `previous_backup_id`, `previous_backup_sha256` z potwierdzonego utworzenia. Przed zapisem MCP zapisze i zweryfikuje kopię konfiguracji Expression. Mostek wywołuje `system.tag.configure()` ze zmianą `valueSource`, `defaultValue` i `value`, tak jak przy Speed, z polityką `m`. Zapisz `configure.count`, `configure.qualities`, `configure.valueBefore` i wynik niezależnego odczytu, w tym `configuration_unchanged`.
3. Porównaj efektywne `valueSource`, wartość, jakość i obecność `expression`. Jeśli źródło to Memory, a pole `expression` pozostało, odczytaj wartość po kilku sekundach na przełomie kolejnych sekund. Potwierdź, czy nadal równa się `configure.valueBefore` mimo zmiany sekund zegara.
4. Po nieudanym MergeOverwrite zatrzymaj zapisy i ustal przyczynę na podstawie QualityCode oraz odczytu stanu. Rozważ `action=overwrite` dopiero po sprawdzeniu kopii, potwierdzeniu niezmienionej konfiguracji i uzyskaniu `overwrite_available=true`. Overwrite używa stanu i danych kopii z nieudanego MergeOverwrite. Zapisuje pełną konfigurację z zachowanymi pozostałymi właściwościami i usuniętym `expression`. Przy niejednoznacznym wyniku lub częściowej zmianie zakończ próbę i przejrzyj odczyt oraz kopię.
5. Przy zatwierdzonym sprzątaniu przywróć Expression przez `tag_config_restore` z kopii skutecznej migracji. Następnie wycofaj utworzenie przez to samo narzędzie, używając kopii utworzenia, `confirm=true` i `exclusive_access=true`. Gdy stan lub pokwitowanie się nie zgadza, pozostaw tag i odnotuj ostatni zweryfikowany stan.

Wynik dotyczy tagu bez historii. Przed zmianą Speed porównaj jego konfigurację, stan i ustawienia historyczne z planem migracji i zachowaj osobną kopię.

## Przygotowanie migracji Speed w Bridge 0.1.9

Aktualizator 0.1.9 zapisuje wyłącznie bibliotekę Bridge. Po wdrożeniu i osobnej zgodzie na migrację użyj `tag_config_get` i istniejącego `tag_configure_scoped` z `action=prepare_cupper`, aktualnym `expected_state`, `confirm=true` i `exclusive_access=true` dla `Speed`.

MCP sprawdza 10-sekundowe próbkowanie `Postgres_Historian` i zapisuje kopię bieżącej konfiguracji oraz skrót próbek z 30-minutowego okna kończącego się pięć minut przed odczytem. Bridge ponownie sprawdza to samo okno, a następnie pobiera bieżącą wartość i używa listy słowników z polityką `m`. Odczyt po zapisie potwierdza Memory / Int4, jakość, przechwyconą wartość, ustawienia Historiana oraz liczbę i skrót próbek z identycznego okna. Instalacja Bridge nie wywołuje operacji na tagach.
