# YalaInsight — cinematic launch video plan

**What it is:** a multi-tenant SaaS that tells solar plant owners how much energy and revenue they lost, why, and which fix pays back best.
**For:** solar asset owners, O&M teams and analysts across African portfolios (KE / ZA / MA in the demo).
**Sets it apart:** every lost kWh is attributed to exactly one cause, then valued in money, then turned into a payback-ranked fix.
**Hook:** "The sun came up. The revenue didn't."
**Share caption:** see share-copy.txt

## Tone & identity
- Tone: cinematic. Trailer pacing, huge type, dramatic wipes (a gold light-sweep wipe in the brand accent).
- Palette from `frontend/styles.css` dark theme: bg #0e1514, panel #152120, ink #e3ecea, brand #2bb39b / #0f6b5c, accent (sun) #e8a33d, bad #c0392b, sidebar #08100f.
- Type: Inter Display Black for trailer cards; the product UI keeps its own stylesheet (system-ui).
- Logo: the app's ◐ glyph in accent gold.

## Music
- 90 BPM, D minor, 4/4. Beat = 0.6667 s = 20 frames at 30 fps. 32 beats = 21.33 s.
- Low drone + sub pulse in the hook, big hit on the logo reveal, driving toms/kick under highlights, final hit + ring-out on outro.
- Whooshes on wipes are filtered noise tuned under the music, all on the beat grid.

## Storyboard (beats → seconds)
| # | Beats | Time | Scene |
|---|---|---|---|
| 1 Hook | 0–6 | 0.00–4.00 | Black, slow sun-disc rise behind type. "The sun came up." (beats 0–3) → "The revenue didn't." in gold (beats 3–6) |
| 2 Reveal | 6–11 | 4.00–7.33 | Gold light-sweep wipe → ◐ YalaInsight, "Solar plant intelligence." |
| 3 Highlight 1 | 11–17 | 7.33–11.33 | Real portfolio dashboard (sidebar + KPI cards from app markup/CSS) — cards land one by one, camera pushes into "Est. gross lost energy value". Card: "See every kWh you lost." |
| 4 Highlight 2 | 17–22 | 11.33–14.67 | Real "Loss attribution (base)" panel — Chart.js bars grow. Card: "Know exactly why." |
| 5 Highlight 3 | 22–27 | 14.67–18.00 | Real recovery simulator — KPIs + cumulative cash flow crosses zero, payback 0.75 yrs. Card: "Fix what pays back first." |
| 6 Outro | 27–32 | 18.00–21.33 | ◐ YalaInsight · "Every kWh, accounted for." · github.com/wemuve/YalaInsight · Deploy to Render in one click |

## Data / privacy
- All figures are the app's synthetic demo data style; a small "Synthetic demo data" label stays on product shots.
- Simulator figures come from running the real `simulate_recovery` engine on the simulator's default inputs (250 MWh/yr loss, 80 % recovery, USD 0.085/kWh, USD 12,000 repair).
- Seed plants use real place names (e.g. Kathu, Benguerir are real solar sites). Swapped for fictional stand-ins: Rift Valley Array (KE), Karoo Sun Farm (ZA), Atlas Ridge Solar (MA). User/org shown as "Demo Analyst · Demo Renewables".
