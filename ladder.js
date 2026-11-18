/* Stop ladder (A2) for open trades.
   R = entry - initial stop.
   Keep the initial stop until +2R. +2R -> stop to entry. +4R -> +2R. +6R -> +4R ... (steps of 2R).
   No profit target, no time limit. A level counts as reached when a day's high touches it; the stop is
   raised from the NEXT day, and the entry day's high is never used.
   If a day's low touches the stop, the trade is stopped out (a gap below the stop exits at that day's open). */
(function (root) {
  function ladderEval(dates, o, h, l, entryDate, entry, initStop, qty) {
    var R = entry - initStop;
    if (!(R > 0)) return { status: "CHECK", note: "The initial stop must be below the entry price." };
    var start = -1;
    for (var i = 0; i < dates.length; i++) { if (dates[i] >= entryDate) { start = i; break; } }
    if (start < 0) return { status: "NO DATA", note: "No price data on or after the entry date." };
    var partial = dates.length > 0 && dates[0] > entryDate; // the entry is older than the stored history
    var stop = initStop, maxM = 0, k = 0, lastDate = null;
    for (var j = start; j < dates.length; j++) {
      if (l[j] == null || h[j] == null || o[j] == null) continue;
      if (l[j] <= stop) {
        var px = k === 0 ? stop : Math.min(o[j], stop);
        return { status: "STOP HIT", exitDate: dates[j], exitPrice: px, finalR: (px - entry) / R,
                 pnl: qty * (px - entry), stop: stop, R: R, partial: partial };
      }
      if (k > 0) maxM = Math.max(maxM, (h[j] - entry) / R);
      if (maxM >= 2) stop = Math.max(stop, entry + (Math.floor(maxM / 2) * 2 - 2) * R);
      lastDate = dates[j];
      k++;
    }
    var nextR = maxM < 2 ? 2 : Math.floor(maxM / 2) * 2 + 2;
    return { status: "OPEN", stop: stop, R: R, bestR: maxM, nextR: nextR,
             nextPrice: entry + nextR * R, stopAfter: entry + (nextR - 2) * R,
             lastDate: lastDate, partial: partial };
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { ladderEval: ladderEval };
  else root.ladderEval = ladderEval;
})(typeof window !== "undefined" ? window : this);
