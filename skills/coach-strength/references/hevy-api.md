# Hevy API Reference

Auth: `api-key` header with key from `.env` (`HEVY_API_KEY`).

| Endpoint | Method | Use |
|----------|--------|-----|
| `/v1/workouts` | GET | Paginated workout list |
| `/v1/workouts` | POST | Create workout |
| `/v1/workouts/{id}` | PUT | Update workout |
| `/v1/workouts/events` | GET | Incremental sync since date |
| `/v1/routines` | GET/POST | List/create routines |
| `/v1/routines/{id}` | PUT | Update routine |
| `/v1/exercise_templates` | GET | Exercise catalog |
| `/v1/exercise_history/{id}` | GET | Per-exercise history |

## Troubleshooting

**"HEVY_API_KEY not found"**
- Add to `.env`: `HEVY_API_KEY=your_key_here`
- Get key at https://hevy.com/settings?developer (requires Hevy Pro)

**Sync shows 0 workouts**
- User needs to log workouts in the Hevy app first
- Check: `curl -H "api-key: $KEY" https://api.hevyapp.com/v1/workouts/count`

**Routine push failed**
- Verify routine exists in Hevy: check `hevy_routine_id` in `coach_routines`
- API may rate-limit — wait and retry
