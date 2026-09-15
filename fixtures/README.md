# Fixtures

`sample_products.json` is a **placeholder** fixture. Its `image_url`s are not real, so
the ingestion / icon / metadata stages will fail on them — that is fine for testing the
review tabs and the state machine, and useless for testing anything that downloads an
image.

Before running any stage, replace it with a real export:

```sql
SELECT p.name_english, p.name_arabic, p.image_url, p.product_url, p.category,
       p.length, p.width, p.height, p.dimension_unit,
       p.price_amount, p.price_unit, s.name_english AS store
FROM (
  SELECT p.*, ROW_NUMBER() OVER (PARTITION BY p.category ORDER BY p.id) AS rn
  FROM core_product p
  WHERE p.is_active AND p.image_url <> ''
    AND p.category IN ('3-seater-sofa','2-seater-sofa','tv-table','carpet','console',
                       'wardrobe','chair','bed','side-table','center-table',
                       'floor-stand','art-canvas','wall-clock','dining-table',
                       'flower-pot-and-plant')
) p
INNER JOIN core_store s ON p.store_id = s.id
WHERE p.rn <= 7
ORDER BY p.category, p.id;
```

Fifteen categories × seven is ~105 products, chosen to cover ten placement roles and
every icon prompt branch: top-down floor items, front-elevation wall items,
`floor-stand`'s nadir prompt, and `dining-table`'s chair-stripping branch.

Then add three bad rows by hand — one with no dimensions, one with a dead image URL,
one with an obviously wrong category — so the failure paths get exercised too.

```bash
python manage.py seed_products --file fixtures/real_export.json --reset
```
