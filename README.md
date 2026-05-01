# posso-volare-zones

Zone di volo per l'app **Posso Volare?** — aggiornate automaticamente ogni giorno da D-Flight.

## Struttura

```
zones/
  italy_zones.json   # Zone UAS Italia (formato ED-269, da D-Flight)
  metadata.json      # Data ultimo aggiornamento e versione
```

## Aggiornamento automatico

Un GitHub Action scarica ogni giorno alle 03:00 UTC le zone aggiornate da D-Flight
usando le credenziali configurate come secrets (`DFLIGHT_USER`, `DFLIGHT_PASS`).

## Secrets richiesti

| Secret | Descrizione |
|--------|-------------|
| `DFLIGHT_USER` | Email account D-Flight |
| `DFLIGHT_PASS` | Password account D-Flight |

## Utilizzo nell'app

L'app Flutter legge `zones/italy_zones.json` e `zones/metadata.json` via
`raw.githubusercontent.com`. Se il repo non è raggiungibile, usa i dati
bundlati nell'app come fallback.
