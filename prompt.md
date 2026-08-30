Make a plan on creating a new script named ./src/corehealth/report.py which creates a single, standalone HTML page with information about the health of the Brazilian MeshCore network.

- The HTML page must use the Pico CSS framework ONLY. The css/, scripts/, and xcss/ are from Pico.
- The generated HTML page must be built inside ./output (which should be a folder ignored by .gitignore)
- The page must be written in Brazilian Portuguese.
- At the moment, the only section that should be in the HTML page is "Anúncios Excessivos", but more will be added in the feature.


# Rules for the "Anúncios Excessivos" section

The "Anúncios Excessivos" section is updated every 24h and must show nodes that are hurting the mesh health somehow. The table should include only nodes whose number of adverts is larger than 2, but whose number of observers that heard this node in the last 24h is larger than 1. This information can be obtained by running the following scripts: 

uv run src/corehealth/top_adverts.py  -t 24h  --repeaters-only --json --all

uv run src/corehealth/reach.py <NODE ID> -t 24h --json
