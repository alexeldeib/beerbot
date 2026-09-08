# Chart geometry regression check

`chart-layout.js` is a read-only browser assertion to evaluate after the personal
dashboard and its selected group have loaded. It checks eight columns, a common
bar baseline/date row, exact proportional heights, zero-height zero values, and
horizontal overflow. Use the browser tool's read-only JavaScript evaluation with
the file's function as the callback.

Check widths 320, 390, 430, 768 and 1280px using both mixed nonzero/zero values and
an empty group. For example, synthetic values `[9,10,0,4,7,9,6,1]` must render
heights `[126,140,0,56,98,126,84,14]` at every width. Do not use production records
or screenshots as committed test fixtures.

This check is currently run during browser QA, not GitHub Actions. Wiring a
headless-browser regression suite into CI is a follow-up; pytest alone does not
validate rendered CSS geometry.
