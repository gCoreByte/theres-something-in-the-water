from statistics import fmean, pstdev

CHANNELS = ("A", "B", "C", "D", "E", "F", "G", "H", "R", "I", "S", "J", "T", "U", "V", "W", "K", "L")
WAVELENGTHS = dict(zip(
    CHANNELS,
    (410, 435, 460, 485, 510, 535, 560, 585, 610, 645, 680, 705, 730, 760, 810, 860, 900, 940),
))
COLORS = dict(zip(CHANNELS, (
    "#8b3dff", "#5a4dff", "#2f74ff", "#00a6ff", "#00c98d", "#3fd43a",
    "#a6dc1e", "#ffd21f", "#ff9b1a", "#ff5a28", "#e8203a", "#c0143c",
    "#a35a6e", "#9a7480", "#8f8590", "#8a8a9a", "#7d7f8f", "#707385",
)))
SATURATED = -1


def channel_stats(samples):
    stats = {}
    for channel in CHANNELS:
        values = [s.values[channel] for s in samples if s.values[channel] != SATURATED]
        if values:
            stats[channel] = {"mean": fmean(values), "std": pstdev(values), "n": len(values)}
        else:
            stats[channel] = {"mean": None, "std": None, "n": 0}
    return stats
