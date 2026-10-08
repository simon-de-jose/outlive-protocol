# Writer payloads

```bash
python3 skills/log-nutrition/scripts/log_nutrition_with_summary.py --json '{
  "provider":"discord",
  "message_id":"<literal source id>",
  "entries":[
    {"event_key":"default","meal_time":"<ISO timestamp>","meal_type":"<type>","meal_name":"<name>","food_items":[],"calories":0,"protein_g":0,"carbs_g":0,"fat_total_g":0,"fat_saturated_g":null,"fiber_g":0,"source":"<used evidence refs and basis>"}
  ]
}'
```

Use one atomic multi-entry call for clear meal boundaries. Do not put `db`, `db_path`, or `profile_path` in writer JSON; test paths are CLI-only.

For resolved unchanged whole-entry reuse:

```bash
python3 skills/log-nutrition/scripts/quick_log_text.py --json '{
  "meal_time":"<ISO timestamp>","meal_type":"<type>",
  "provider":"discord","message_id":"<literal source id>",
  "reuse_mode":"exact","reuse":{"mode":"exact","entry_id":123}
}'
```

