# Recipe Management

Recipes are saved ingredient lists with cached USDA data. They make logging repeated meals instant.
Stored at: `<data>/recipes.json`

## Recipe JSON Format

```json
{
  "id": "latte",
  "name": "Latte",
  "servings": 1,
  "notes": "Double shot, steamed whole milk",
  "created": "2026-02-08",
  "ingredients": [
    {
      "name": "Whole milk",
      "portion_g": 240,
      "usda_fdc_id": 746782,
      "nutrients_per_portion": {
        "calories": 149, "protein_g": 8.0, "carbs_g": 11.4,
        "fat_g": 7.9, "saturated_fat_g": 4.6,
        "fiber_g": 0, "sugar_g": 11.4, "sodium_mg": 105, "cholesterol_mg": 24
      }
    }
  ],
  "totals_per_serving": { "..." : "..." }
}
```

## Creating a Recipe

**Trigger:** "build a recipe," "save a recipe," "create recipe for X"

1. **Gather info:** Recipe name, servings (default: 1), optional notes
2. **Collect ingredients:** Name + amount in natural units, convert to grams for storage
   - Common conversions: 1 cup milk = 244g, 1 large egg = 50g, 1 tbsp olive oil = 14g, 1 slice bread ≈ 30g
3. **USDA lookup per ingredient:** Search → pick best match (prefer "SR Legacy"/"Foundation") → get FDC ID → pull full nutrients
4. **Present:** Per-ingredient and per-serving totals table
5. **Confirm & save** to `<data>/recipes.json`

## Editing/Listing Recipes
- **Edit:** Load recipe → apply changes → re-lookup USDA if ingredient changed → recalculate → save
- **List:** Show all recipe names with brief description
