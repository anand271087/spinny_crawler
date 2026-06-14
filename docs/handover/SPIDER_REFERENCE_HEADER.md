# Spinny OEM Spare-Parts Crawler — Spider Reference

This document collates how **all 26 brand spiders** work in one place. It has two parts:

1. **Summary table** — at-a-glance: each brand's URL, crawling pattern, fields extracted, and whether it needs a login.
2. **Per-brand technical notes** — the detailed write-up for every spider (site reality, selectors/APIs used, gotchas, verified run results). These are reproduced from the project's living `docs/per_site_notes.md`.

**How a spider is structured (read once):** every spider lives in `spiders/<brand>.py`, subclasses `BaseSpider`, and implements `crawl() -> list[Row]`. The framework auto-fills `brand`, `source_website`, `crawl_date`, and `crawl_status`. Per-brand output drops empty columns; the consolidated master keeps the full schema and de-duplicates per BRD §5.

**Six crawling patterns** are used across the 26 brands:

- **flat_list** — one big product list, paginate and read.
- **multi_level_category** — walk a category tree to its leaves.
- **cascading_dropdown** — Make → Model → Variant style selectors; we capture the site's own XHR/API rather than clicking every combination.
- **pdf_brochure** — the catalogue is a downloadable PDF; we parse tables/text out of it.
- **hidden_nav** — the catalogue is behind a hamburger/off-canvas menu we trigger programmatically.
- **snapon_epc / dealer EPC** — authenticated OEM electronic parts catalogues (Hyundai, Toyota, Mahindra, MG, Tata, Ford) reached via captured API tokens or driven UI.

## Summary of all 26 spiders

| Brand | Pattern | Fields extracted | Login? |
|---|---|---|---|
| Maruti | json_api_pagination | item_name, item_code, mrp | No (public OEM) |
| Hyundai | snapon_epc (REST) | item_name, item_code, mrp, compatible_car_model | **Yes** |
| Toyota | snapon_epc (REST) | item_name, item_code, mrp, compatible_car_model, description | **Yes** |
| Mahindra | dealer EPC (Intelli Catalogue) | item_name, item_code, compatible_car_model, description, part_structure | **Yes** |
| MG | dealer EPC (Intelli Catalogue) | item_name, item_code, compatible_car_model, description, part_structure | **Yes** |
| Tata | dealer EPC (ASP.NET) | item_name, item_code, mrp, compatible_car_model | **Yes** |
| Ford | dealer EPC (Microcat REST) | item_name, item_code, compatible_car_model | **Yes** |
| HELLA | multi_level_category | item_name, item_code, mrp | No |
| Uno Minda | multi_level_category | item_name, item_code, mrp | No |
| Technix | flat_list | item_name, item_code, mrp | No |
| Gabriel | pdf_brochure | item_name, item_code, mrp | No |
| Zip | multi_level_category | item_name, item_code, mrp | No |
| Monroe | flat_list | item_name, item_code, mrp | No |
| Amaron | cascading_dropdown | item_name, item_code, mrp | No |
| SF Sonic | cascading_dropdown | item_name, item_code, mrp | No |
| Exide | pdf_brochure (MRP list) | item_name, item_code, mrp | No |
| Spark Minda | multi_level_category | item_name, item_code, mrp | No |
| Schaeffler | json_api_pagination (Spartacus OCC) | item_name, item_code, mrp | No |
| Autokoi | hidden_nav | item_name, item_code, mrp | No |
| Bosch | pdf_brochure | item_name, item_code, mrp | No |
| Valeo | rest_api (TecAssist) | item_name, item_code, compatible_car_model | No |
| ZF | json_api_pagination | item_name, item_code, compatible_car_model | No |
| Lumax | pdf_brochure | item_name, item_code (+mrp bonus) | No |
| JK Tyre | multi_level_category | item_name, compatible_car_model, tyre_sizes | No |
| TVS Girling | cascading_dropdown (2 URLs merged) | item_name, item_code, mrp, vehicle_compatibility | No |
| Mobil | flat_list (Coveo API) | item_name | No |

**Notes on "partial" status:** some brands legitimately don't expose every field on the public site (e.g. Schaeffler/Bosch MRP, Toyota MRP). Those rows are marked `partial` by design — the field is left blank, not invented. Where a field was later cracked (e.g. Hyundai MRP at ~76% coverage), it's noted in that brand's section below.

---

# Per-brand technical notes

The remainder of this document is the detailed per-spider documentation.
