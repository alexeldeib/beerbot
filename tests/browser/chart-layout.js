// Evaluate this read-only function in the browser after the dashboard loads.
// Run at 320, 390, 430, 768 and 1280px with mixed values and with all zeroes.
function checkChartLayout() {
  const columns = [...document.querySelectorAll("#trend .week")];
  if (columns.length !== 8) throw new Error("Expected eight loaded chart columns");
  const data = columns.map(column => {
    const bar = column.querySelector(".bar").getBoundingClientRect();
    const plot = column.querySelector(".week-plot").getBoundingClientRect();
    const label = column.querySelector(".week-date").getBoundingClientRect();
    const value = Number(column.querySelector("strong").textContent.replaceAll(",", ""));
    return {value, height: bar.height, bottom: bar.bottom, plotBottom: plot.bottom, labelTop: label.top};
  });
  const max = Math.max(1, ...data.map(row => row.value));
  const close = (a, b) => Math.abs(a - b) < 0.75;
  for (const row of data) {
    if (!close(row.bottom, data[0].bottom)) throw new Error("Bar baselines differ");
    if (!close(row.bottom, row.plotBottom)) throw new Error("Bar is not bottom aligned");
    if (!close(row.labelTop, data[0].labelTop)) throw new Error("Date rows differ");
    if (!close(row.height, Math.max(0, row.value) / max * 140)) throw new Error("Bar height was distorted");
  }
  const chart = document.getElementById("trend");
  if (chart.scrollWidth > chart.clientWidth + 1) throw new Error("Chart overflows horizontally");
  return {width: innerWidth, values: data.map(row => row.value), heights: data.map(row => row.height), aligned: true};
}
