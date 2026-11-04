# Popusti namirnica

Python skripta koja se svakog dana izvršava na GitHub Actions i putem Discord-a obaveštava kada je cena običnog pilećeg filea u nekom od većih marketa u Srbiji pala ispod zadatog praga.

## Izvor podataka

Trgovci su po *Pravilniku o uslovima, sadržaju i načinu objavljivanja cenovnika* dužni da svakog dana objavljuju mašinski čitljive cenovnike. Oni su popisani na portalu otvorenih podataka [data.gov.rs](https://data.gov.rs/sr/datasets/?tag=cenovnik). Svi fajlovi su u istom formatu: CSV sa separatorom `;`, UTF-8 i 15 propisanih kolona.

Skripta trenutno čita cenovnike sledećih lanaca:

| Lanac | Prodavnice |
|---|---|
| Maxi (Delhaize) | Maxi, Mega Maxi, Shop&Go, Profi, Maxi Online |
| Idea | IDEA, Mercator, Roda |
| Univerexport | Univerexport S/M/L |
| Lidl | Lidl |
| Gomex | Gomex |
| Aman | Aman |
| Super Vero | Super Vero |
| METRO | METRO Cash&Carry |

Ukupno je to oko 100 MB, a preuzimanje i obrada traju desetak sekundi. Izvori se podešavaju u listi `SOURCES` u `main.py`. Izvor može biti fiksni URL ili *slug* dataseta na data.gov.rs; u drugom slučaju skripta preko API-ja nalazi najnoviji fajl.

## Kako se prepoznaje proizvod

Svaki lanac drugačije imenuje proizvode, pa skripta:

1. uzima samo važeći cenovnik (`VAZECI_CENOVNIK`) i kategoriju 8, „Sveže i prerađeno meso”;
2. normalizuje naziv (mala slova, bez dijakritika) i traži „pil...” i „file”;
3. odbacuje prerađene i pripremljene proizvode (dimljeni, slajs, delikates, u crevu, u omotu, panirani, marinirani, zamrznuti, batak/karabatak...). Spisak je u `EXCLUDE_PATTERNS`.

Cena po kilogramu se računa iz težine u nazivu („480 gr”, „800G”), jer kolona „Cena po jedinici mere” nije pouzdana kod svih trgovaca. Artikli „cca 1kg” koji se prodaju na kg već imaju cenu po kilogramu. Ako je akcija aktivna, koristi se snižena cena.

## Podešavanje

U *Settings → Secrets and variables → Actions* repozitorijuma:

| Promenljiva | Opis |
|---|---|
| `THRESHOLD_PRICE` (variable) | prag u **RSD/kg**; javljaju se ponude po toj ceni ili nižoj |
| `DISCORD_WEBHOOK_URL` (secret) | URL Discord webhook-a na koji stižu poruke |

Workflow se pokreće svakog dana u 08:00 UTC, a može se pokrenuti i ručno: *Actions → Popusti namirnica → Run workflow*.

Lokalno pokretanje:

```bash
pip install -r requirements.txt
THRESHOLD_PRICE=750 DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/... python main.py
```

### Literatura

https://www.python-engineer.com/posts/run-python-github-actions/

https://dev.to/this-is-learning/secrets-and-environment-variables-in-your-github-action-1pfn

https://stackoverflow.com/questions/65957197/difference-between-githubs-environment-and-repository-secrets
