# Odczyt surowej historii przez MCP

`history_query_raw(path, start_ms, end_ms)` wywołuje odczyt `history/raw`
w Bridge 0.1.10. Granice są w milisekundach Unix i obie należą do przedziału.
Wynik zawiera `path`, `start_ms`, `end_ms`, `count`, `sha256` oraz `samples`.
Każda próbka zawiera `timestamp_ms`, `value`, `quality` i `path`. Jakość jest
zwracana jako tekstowa reprezentacja wartości z Historiana.

Bridge sprawdza dokładną ścieżkę na liście `RAW_HISTORY_PATHS` w
[fragmencie WebDev](../bridge/src/raw_history.py). Lista łączy ścieżkę taga
ze ścieżką odpowiedniego providera historii. Pierwszy wpis obejmuje wyłącznie
`[default]Line_1/Cupper/Ignition/Speed`. Dla SQL Historian ścieżka kwalifikowana
używa `drv`. Odczyt nie wymaga `IGNITION_ALLOW_WRITES`.

Bridge używa `system.historian.queryRawPoints` z `TALL` i `returnSize=-1`.
W SQL Historian funkcja czyta surowe partycje i nie używa zagregowanych
partycji. Bridge odrzuca rekordy spoza żądanych granic. Dla Speed ustawienie
`Discrete` dodatkowo wyklucza wartości dodawane na granicach. Ignition 8.3.9
zachowuje argument `includeBounds` dla zgodności. Okno przekraczające
7 dni albo wynik przekraczający 10 000 próbek kończy się błędem. Operacja
zachowuje duplikaty dostarczone przez Historian.
[Dokumentacja Ignition 8.3](https://docs.inductiveautomation.com/docs/8.3/appendix/scripting-functions/system-historian/system-historian-queryRawPoints)
[Konfiguracja Historiana](https://docs.inductiveautomation.com/docs/8.3/ignition-modules/tag-historian/configuring-tag-history)
[Partycje SQL Historian](https://docs.inductiveautomation.com/docs/8.3/ignition-modules/tag-historian/tag-history-providers)

SHA-256 obejmuje wszystkie próbki. MCP sortuje je według czasu, a remis
rozstrzyga tekst zwartego JSON z posortowanymi kluczami. Oblicza skrót
z rekordów w tej kolejności, oddzielonych znakiem LF bez końcowego LF.

## Wdrożenie na testowym Gateway

Polecenie wdrożenia uruchom z katalogu `ignition-scada`.

```sh
./.local/start-ignition-mcp.sh --upgrade-bridge-0110 --apply
```

Aktualizator najpierw potwierdza Bridge 0.1.9 i tworzy zweryfikowaną kopię
biblioteki w `backups/mcp-bridge/`. Po zapisie weryfikuje wersję, listę operacji
i zawartość biblioteki niezależnym eksportem projektu. Odśwież połączenie MCP,
aby klient wczytał nową listę narzędzi z `.codex/config.toml`.

## Odczyt i porównanie

Poniższy test wykonuje dwa odczyty przez serwer MCP i zapisuje obie odpowiedzi.
Okno odpowiada 24.11.2026 od 19:00:53.427 do 19:30:53.427 CET.

```sh
./.local/ignition-mcp/.venv/bin/python -B .local/ignition-mcp/tests/live/check_raw_history.py \
  '[default]Line_1/Cupper/Ignition/Speed' 1795543253427 1795545053427 \
  --save-to backups/speed-history-before.json
```

Po migracji użyj tych samych granic, `--baseline backups/speed-history-before.json`
i nowej ścieżki w `--save-to`. Test porówna oba odczyty z zapisanym odczytem
sprzed migracji rekord po rekordzie i według SHA-256. Dodatkowe okno po czasie
migracji służy do potwierdzenia napływu nowych próbek.
