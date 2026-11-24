# Mostek MCP 0.1.4 dla Ignition 8.3.9

`tag_cupper_validate` sprawdza ścieżkę Cuppera, operację `prepare` lub
`restore` oraz jawne `exclusive_access=true`. WebDev dekoduje podpisany
ładunek Cuppera standardowym `json.loads` po weryfikacji HMAC. Walidator
współdzielony z zapisem wymaga wartości logicznej `True`. Odczytowa trasa
walidacyjna sprawdza też foldery taga i kończy się przed odczytami potrzebnymi
do migracji oraz przed zapisem.

`tag_cupper_production_history` wymaga obu granic `start_ms` i `end_ms`
jako liczb całkowitych w milisekundach Unix. Okno może obejmować do siedmiu
dni i uwzględnia obie granice. Każda próbka zawiera `timestamp_ms`, `value`,
`quality` i `path`. Jakość jest tekstową reprezentacją pola jakości
z Historiana lub `null`, jeśli pole nie ma wartości.

Mostek przekazuje granice jako obiekty Date do `queryRawPoints` z
`returnFormat="TALL"` i `returnSize=-1`. Dokumentacja 8.3 ogranicza obsługę
parametru `returnSize` do Internal i Core Historian. Trasa zwraca wszystkie
wiersze SQL Historian ze wskazanego okna, łącznie z identycznymi wierszami.
Sortuje według czasu i kanonicznego JSON próbki, a skrót SHA-256 obejmuje
wiersze JSON połączone znakiem nowej linii. Dotychczasowy format skrótu
pozostaje dostępny dla kopii migracyjnych 0.1.3.
[Dokumentacja queryRawPoints dla Ignition 8.3](https://www.docs.inductiveautomation.com/docs/8.3/appendix/scripting-functions/system-historian/system-historian-queryRawPoints)

Z katalogu `ignition-scada`, po uzyskaniu zgody na aktualizację biblioteki
WebDev, uruchom istniejący launcher z poświadczeniami z mechanizmu sekretów.

```sh
./.local/start-ignition-mcp.sh --upgrade-bridge-014 --apply
```

Aktualizacja porównuje eksport projektu z podpisanym odczytem biblioteki
i sprawdza możliwość odtworzenia kopii bez utrwalania `BRIDGE_SECRET`.
Zapis obejmuje bibliotekę mostka i skan projektów. Po skanie skrypt sprawdza
wersję, cztery konfiguracje tagów Cuppera, prawidłowe i odrzucone żądania
walidacyjne oraz trzy odczyty historii. Dwa odczyty okna
`1794866042408–1795470842408` porównuje próbka po próbce i według SHA-256.
Trzeci odczyt obejmuje wcześniejsze okno. Argument `--verify` uruchamia samą
kontrolę odczytową. Po aktywacji mostka ponownie połącz lokalnego klienta MCP,
aby wczytał aktualny schemat narzędzi.
