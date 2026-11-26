# Aktualizacja mostka mcp-bridge do 0.1.1

Aktualizacja obejmuje wyłącznie
`mcp-bridge/ignition/script-python/mcp_bridge_lib/code.py`. Patch powstał
z eksportu działającego projektu. Zachowuje przypisanie `BRIDGE_SECRET`,
weryfikację HMAC, wszystkie dotychczasowe trasy oraz pliki endpointu WebDev `api`.

## Materiały

- `mcp-bridge-0.1.0-1795467508565.zip.enc` to zaszyfrowany eksport 0.1.0.
  SHA-256 szyfrogramu to
  `27a1710735e6f811b88ad9279156c403aa6848f40f990487caaa2e83d4104190`.
- `mcp-bridge-0.1.1-1795467508565.patch` to poprawka do przeglądu, bez sekretu.
- `bridge/upgrade_011.py` i `bridge/baselines/mcp_bridge_lib_0_1_2.py` to narzędzie i lokalny
  wzorzec zmian. Na hoście Gateway można umieścić je w tym samym katalogu
  roboczym i jawnie przekazać `--candidate`.

Narzędzie odszyfrowuje kopię dotychczasowym sekretem mostka, pobieranym do
pamięci z istniejącej biblioteki Gateway. Zaszyfrowane archiwum przeszło
weryfikację integralności, odszyfrowanie i porównanie z drugim eksportem.

## Podpisana aktualizacja z Maca

Uruchom w Terminalu na Macu, w środowisku z dostępem do poświadczeń używanych
przez launcher MCP:

```sh
/Users/pietia/Development/ignition-scada/.local/start-ignition-mcp.sh --upgrade-bridge-011
```

Jednorazowy skrypt używa `BridgeClient` do `files/read` i `files/write` oraz
`GatewayClient` do eksportu i skanu. Sprawdza kopię, odczytuje bibliotekę
tuż przed zapisem i potwierdza jej SHA-256 po zapisie. Po skanie oczekuje na
podpisany odczyt wersji `0.1.1` i nowych operacji. Dane biblioteki są przetwarzane
w pamięci. Raport zawiera wynik, skróty i wersję mostka.

Wynik `success` oznacza zgodny odczyt pliku oraz aktywną wersję 0.1.1.
Wynik `already_upgraded` oznacza, że plik i aktywna wersja już się zgadzają.
Przy `uncertain` odczytaj pola `observed.library` oraz
`observed.runtime_version`. Skrypt sprawdza stan po błędzie i nie wykonuje
ponownego zapisu w ramach tej samej operacji. Jeśli gotowy plik jest już na
Gateway, a nadal działa 0.1.0, następne uruchomienie po potwierdzeniu tego
stanu wykonuje skan bez ponownej podmiany pliku. Jeśli stan pliku odbiega od
obu znanych wersji, przejdź do procedury cofnięcia z administratorem.

## Aktywacja na hoście Gateway

1. Administrator Gateway z uprawnieniami do aktualizacji plików projektów
   kopiuje cztery materiały do zabezpieczonego katalogu roboczego na hoście.
   Potrzebne są Python 3.9+ i `openssl` dostępny w `PATH`. W PowerShellu ustaw
   `$ProjectsDir` na rzeczywisty katalog `data\projects` tej instalacji.
2. Uruchom sprawdzenie bez zapisu. Narzędzie odszyfruje kopię w pamięci, porówna
   pliki endpointu z kopią, potwierdzi dokładną treść bieżącej biblioteki
   i odtworzy przygotowany patch.

   ```powershell
   $BridgeUpdate = @(
       '-B', '.\upgrade_011.py', 'activate',
       '--projects-dir', $ProjectsDir,
       '--backup', '.\mcp-bridge-0.1.0-1795467508565.zip.enc',
       '--patch', '.\mcp-bridge-0.1.1-1795467508565.patch',
       '--candidate', '.\mcp_bridge_lib_0_1_2.py'
   )
   py -3 @BridgeUpdate
   ```

3. Po wyniku `write_applied=false` i `target_version=0.1.1` uruchom
   `py -3 @BridgeUpdate --write`. Narzędzie sprawdzi stan ponownie, podmieni
   wyłącznie `code.py` i odczyta zapisany plik. W Windows podmiana korzysta
   z operacji zachowującej ACL oryginalnego pliku. [Dokumentacja ReplaceFileW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-replacefilew)
4. Administrator uruchamia **Scan File System** w **Platform → System →
   Projects** po upewnieniu się, że inne projekty nie mają oczekujących zmian
   plikowych. Oczekiwany wynik `ignition_diagnose` to Bridge `v0.1.1` i
   poprawne uwierzytelnienie. [Skan plików projektów w Ignition 8.3](https://docs.inductiveautomation.com/docs/8.3/tutorials/version-control-guide)
5. Po ponownym połączeniu klienta MCP odczytaj `bridge_status`. Sprawdź
   `tags/scoped-state`, `tags/scoped-configure`, `tags/scoped-restore` oraz
   `files/list`, `files/read`, `files/write`, `files/delete`. Potwierdź
   możliwość odczytu widoków i stron `Test_Gemma_v1` oraz odczyt taga
   `[default]Line_1/Cupper/Ignition/Status` przez `tag_config_get`.

## Cofnięcie aktualizacji

Jeśli weryfikacja po skanie się nie powiedzie, przygotuj dodatkowy zaszyfrowany
eksport bieżącego `mcp-bridge` za pomocą `capture --backup-only` i prywatnego
FIFO. Następnie uruchom `py -3 @BridgeUpdate --rollback`. Narzędzie sprawdzi,
czy bieżący plik jest dokładnie przygotowaną wersją 0.1.1.

Po wyniku `target_version=0.1.0` użyj polecenia:

```powershell
py -3 @BridgeUpdate --rollback --write --current-backup .\mcp-bridge-current.zip.enc
```

Wykonaj skan oraz podpisany odczyt statusu.

## Narzędzia MCP

Lokalny launcher ogranicza `IGNITION_TAG_CONFIG_ALLOWLIST` do
`[default]Line_1/Cupper/Ignition`. W `.codex/config.toml` lista `enabled_tools`
zachowuje narzędzia projektu i Perspective oraz dodaje `bridge_status`,
`tag_config_get`, `tag_configure_scoped` i `tag_config_restore`.
Klient wczytuje tę listę przy ponownym połączeniu. Operacje modyfikujące
wymagają `confirm=true` oraz przechodzą kontrolę ścieżki, stanu i kopii.
