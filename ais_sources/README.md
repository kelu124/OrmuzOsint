# AIS Data Sources

A curated list of AIS data sources relevant to maritime OSINT, dark-vessel detection, and SAR-AIS fusion. Entries are ordered roughly from most accessible (free, no signup) to most capable (paid/commercial).

---

## 1. aisstream.io

- **URL:** https://aisstream.io
- **Type:** Real-time (WebSocket)
- **Coverage:** Global (AIS base stations + satellite AIS aggregated)
- **Cost:** Free
- **Notable limitations:** Server-side bbox filtering is available but imprecise — always apply a client-side filter. Mid-ocean and remote strait coverage depends on satellite AIS uplink cadence (~minutes of latency). No historical replay via the WebSocket.

---

## 2. MarineTraffic

- **URL:** https://www.marinetraffic.com / https://www.marinetraffic.com/en/ais-api/getting-started/
- **Type:** Real-time + historical
- **Coverage:** Global (dense terrestrial + satellite AIS)
- **Cost:** Free web viewer; API from ~$50/month. Historical data requires paid tier.
- **Notable limitations:** API is rate-limited and expensive for bulk pulls. Free tier is view-only with no programmatic access. One of the most complete commercial terrestrial AIS networks.

---

## 3. VesselFinder

- **URL:** https://www.vesselfinder.com / https://api.vesselfinder.com
- **Type:** Real-time + historical
- **Coverage:** Global
- **Cost:** Free web viewer; API plans from ~$30/month
- **Notable limitations:** Similar to MarineTraffic. API has per-vessel and per-request quotas. Free plan covers only a handful of vessels.

---

## 4. AISHub

- **URL:** https://www.aishub.net
- **Type:** Real-time (feed sharing network)
- **Coverage:** Global (community-contributed terrestrial receivers)
- **Cost:** Free — but you must contribute your own AIS receiver feed to access the aggregated stream
- **Notable limitations:** Requires running your own RTL-SDR / VHF receiver and sharing the feed. Coverage is patchy in areas with few contributors (e.g. mid-Hormuz). Data quality varies by contributor.

---

## 5. Kystverket (Norwegian Coastal Administration)

- **URL:** https://www.kystverket.no/en/navigation-and-monitoring/ais/access-to-ais-data/
- **Type:** Real-time + historical
- **Coverage:** Norwegian territorial waters and EEZ
- **Cost:** Free for research and non-commercial use
- **Notable limitations:** Norway only. Historical data available via REST API after registration. Excellent quality for the North Sea / Barents Sea; not useful for Hormuz.

---

## 6. Danish Maritime Authority (DMA)

- **URL:** https://www.dma.dk/safety-at-sea/navigational-information/ais-data
- **Type:** Historical (bulk download)
- **Coverage:** Danish waters and North Sea
- **Cost:** Free (registration required)
- **Notable limitations:** Denmark only. Data provided as CSV bulk exports by month. No real-time feed. Good for retrospective analysis.

---

## 7. Global Fishing Watch (GFW)

- **URL:** https://globalfishingwatch.org/data-download/ / https://globalfishingwatch.org/our-apis/
- **Type:** Historical (processed AIS + satellite AIS)
- **Coverage:** Global, focused on fishing vessels
- **Cost:** Free for non-commercial research (registration + data use agreement required). API available.
- **Notable limitations:** Focuses on fishing vessel behavior and IUU fishing. Vessel positions are processed/interpolated — not raw AIS. Excellent for dark fishing vessel analysis. Coverage gaps in areas with few satellite passes.

---

## 8. Spire Maritime

- **URL:** https://spire.com/maritime/
- **Type:** Real-time + historical (satellite AIS)
- **Coverage:** Global (constellation of ~100+ LEO satellites)
- **Cost:** Commercial — pricing on request, typically thousands of dollars/month for real-time global feeds
- **Notable limitations:** Best-in-class satellite AIS latency (~minutes globally). Message decode rate is highest of any commercial provider. Not accessible for personal/research use without a contract.

---

## 9. exactEarth / Orbcomm

- **URL:** https://www.exactearth.com / https://www.orbcomm.com/en/networks/ais
- **Type:** Real-time + historical (satellite AIS)
- **Coverage:** Global
- **Cost:** Commercial (contact for pricing)
- **Notable limitations:** exactEarth was acquired by Orbcomm. Strong historical archive going back to 2009. Widely used by intelligence agencies and insurance. Expensive and requires contracts.

---

## 10. Kpler

- **URL:** https://www.kpler.com
- **Type:** Real-time + historical (AIS + satellite AIS + port calls)
- **Coverage:** Global, focused on commodity flows (oil, LNG, dry bulk)
- **Cost:** Commercial — enterprise pricing, typically $50k+/year
- **Notable limitations:** Not just raw AIS — fused with port call data, cargo estimates, satellite imagery. Widely used by energy traders and sanctions analysts. Hormuz tanker tracking is a core use case. No individual researcher access.

---

## 11. TankerTrackers.com

- **URL:** https://tankertracker.com
- **Type:** Real-time monitoring + historical (AIS + SAR fusion)
- **Coverage:** Global, focused on tankers and sanctioned fleets
- **Cost:** Reports and subscriptions from ~$500/month; custom research available
- **Notable limitations:** Commercial open-source intelligence service, not a raw data API. Specifically relevant for Hormuz — tracks IRGC tankers, flag-of-convenience vessels, ship-to-ship transfers. Their methodology overlaps with this toolkit's goals.

---

## 12. UN COMTRADE / ITU Ship Stations

- **URL:** https://www.itu.int/en/ITU-R/terrestrial/Pages/mmsi.aspx
- **Type:** Reference (MMSI → vessel identity lookup)
- **Coverage:** Global registry
- **Cost:** Free (MMSI lookup); bulk data requires formal request
- **Notable limitations:** Useful for cross-referencing MMSI numbers found in AIS against the ITU radio station database. Not a live feed — a reference for vessel identification.

---

## 13. OpenCPN + Receiver Stack (DIY)

- **URL:** https://opencpn.org / https://github.com/jvde-github/AIS-catcher
- **Type:** Real-time (your own VHF receiver)
- **Coverage:** Line-of-sight from your antenna (~40–80 km terrestrial; ~400 km elevated)
- **Cost:** Hardware: RTL-SDR v3 (~$25) + VHF antenna (~$30). Software: free.
- **Notable limitations:** Terrestrial-only. For Hormuz you'd need a receiver on the coast of Oman or UAE (or a ship). AIS-catcher decodes AIVDM/AIVDO and can push NMEA over TCP — directly compatible with `ais_listener.py --sources nmea-tcp`. Excellent for local high-density capture.

---

## 14. SatNOGS / Amateur Satellite AIS

- **URL:** https://satnogs.org
- **Type:** Real-time (amateur satellite network)
- **Coverage:** Spotty global (depends on satellite passes and ground station schedule)
- **Cost:** Free
- **Notable limitations:** SatNOGS can receive AIS from LEO satellites (e.g. the NORSAT series) but is primarily a telemetry network. AIS decode is opportunistic. Not reliable for operational coverage; interesting for experimentation.

---

## 15. ORBCOMM AIS (Historical Archive via AWS)

- **URL:** https://registry.opendata.aws/ (search "AIS")
- **Type:** Historical (bulk)
- **Coverage:** Global
- **Cost:** Storage costs only (data transfer from S3)
- **Notable limitations:** ORBCOMM publishes historical AIS snapshots on AWS Open Data. Coverage and recency vary by dataset. Good for bulk retrospective analysis without a commercial contract.

---

## Coverage note for Hormuz

Terrestrial AIS coverage in the Strait of Hormuz is incomplete. The narrowest point (~54 km) is well within range of shore-based receivers on both the Omani and Iranian coasts, but:

- Iran does not operate a public AIS aggregation network.
- Oman's Khasab base is the main western source; coverage fades east of ~56°E.
- Mid-strait and deep Gulf coverage depends almost entirely on satellite AIS.

For this project's SAR-AIS fusion workflow, **aisstream.io** (free, global WebSocket) is the default. For higher completeness on the sanctioned/dark-fleet problem, the commercial satellite AIS services (Spire, exactEarth/Orbcomm, Kpler) are the operational gold standard — but are out of reach for individual researchers.
