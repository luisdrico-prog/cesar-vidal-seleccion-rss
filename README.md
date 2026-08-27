# La Voz de César Vidal — RSS selectivo

Este proyecto crea un RSS combinado con **solo** estas tres secciones del podcast de iVoox:

- Editorial
- Despegamos
- Así fue España

Ignora automáticamente el resto del contenido del podcast.

## Repositorio recomendado

`cesar-vidal-seleccion-rss`

## Primera ejecución

La primera ejecución recorre el archivo histórico del podcast y puede tardar bastante más que las siguientes.

Después, las actualizaciones son incrementales y se ejecutan cada 6 horas.

## Archivos generados

- `catalog.json`
- `feed.xml`
- `backfill_state.json`

## Feedly

Una vez creado el repositorio bajo `luisdrico-prog`, la URL será:

`https://raw.githubusercontent.com/luisdrico-prog/cesar-vidal-seleccion-rss/main/feed.xml`

## Histórico de Feedly

El feed expone permanentemente los 90 episodios más recientes y además rota 120 históricos cada 24 horas para facilitar que Feedly indexe progresivamente el archivo antiguo.
