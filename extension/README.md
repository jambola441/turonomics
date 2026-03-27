# Turonomics — Chrome Extension

Exports a CSV of your Turo host trips from the last 90 days.

## Output CSV format

```
trip_id,start_time,end_time,license_plate
12345,2024-01-15T10:00:00Z,2024-01-18T14:00:00Z,ABC1234
```

| Column | Description |
|--------|-------------|
| `trip_id` | Turo reservation/trip ID |
| `start_time` | Trip start (ISO 8601) |
| `end_time` | Trip end (ISO 8601) |
| `license_plate` | Vehicle license plate (uppercase, no spaces) |

## Development

```bash
npm install
npm run build        # compile TypeScript → dist/
npm run watch        # watch mode
npm run lint         # eslint
```

## Loading in Chrome

1. Run `npm run build`
2. Open Chrome → `chrome://extensions/`
3. Enable **Developer mode**
4. Click **Load unpacked** → select this `extension/` directory

## Usage

1. Navigate to `https://turo.com/us/en/host-dashboard/trips`
2. Click the Turonomics extension icon
3. Click **Export Last 90 Days**
4. The CSV downloads automatically

## Updating Selectors

If Turo changes their UI markup, update the `SELECTORS` object at the top of
`src/content.ts`. Use Chrome DevTools on the host dashboard trips page to
find the correct CSS selectors for trip rows, start time, end time, and
license plate elements.
