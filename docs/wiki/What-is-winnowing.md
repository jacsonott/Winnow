# What is winnowing?

Winnowing is the old farm job of separating grain from chaff, and Winnow is
named for it. This page explains the farm version for anyone who has never
seen it done, and why it describes what Winnow is for.

![The launch animation partway through: event IDs, ports and process names blowing off to the right, while the grain that dropped out of the stream spells WINNOW](../screenshots/splash.png)

## Grain and chaff

A cereal crop such as wheat, barley, oats or rice isn't ready to eat when
it's cut. Each kernel, the **grain**, is still wrapped in a dry husk and
mixed in with bits of stalk, straw and dust. All of that light, papery
waste is the **chaff**.

**Threshing** comes first: the crop is beaten with flails, trodden by
animals or rolled over until the kernels break loose. That frees the grain,
but it doesn't sort anything. What's left is one heap, grain and chaff
mixed together.

## How winnowing works

Winnowing sorts that heap with moving air. The mix is tossed up into the
wind, traditionally with a wooden winnowing fork or from a wide, shallow
basket. Grain is heavy, so it falls almost straight back down. Chaff is
light, so the wind catches it and carries it off. Toss the heap enough
times and what's left on the ground is clean grain.

Farmers did this on a threshing floor, a hard, flat patch of ground on a
rise or between a barn's two big doors, wherever the breeze could be
counted on. Later, fanning mills made their own wind with a
hand-cranked fan, and a modern combine harvester threshes and winnows in
one pass, with a fan inside it blowing the chaff out the back.

The word itself comes from the Old English for *wind*, and the idea stuck
in everyday English. To "separate the wheat from the chaff" is to keep what
is valuable and throw out the rest, and to "winnow down" a list is to cut
it to the few entries worth keeping.

## Why a log viewer is named for it

A triage collection is the heap on the threshing floor. An EvtxECmd export
of one server's event logs can run to millions of rows, and nearly all of
them are routine: service logons, scheduled tasks, update checks, the same
process starting every few minutes. The few rows that matter, such as the
first logon from an unfamiliar address, a service installed at 3 a.m. or
the log that was cleared, look just like the rest until you sift for them.

Winnow is the threshing floor and the wind. Filters blow away what you
already know is noise, and what falls back is what you read, tag and hand
on.

| On the threshing floor | In Winnow |
| --- | --- |
| The harvested heap | The tables you import into a case |
| Tossing it into the wind | Filtering, searching and sorting |
| The chaff blowing away | The rows a filter hides, such as `!svchost` |
| The grain that falls back | The rows you tag **TA** or **Suspicious** |
| The clean grain, bagged | The **Timeline** tab, and **Case ▾ → Export…** |

One thing works differently from the farm: nothing is thrown away. A filter
hides rows rather than deleting them, and the evidence files are never
modified, so clearing a filter brings all the chaff back. That matters,
because triage is never one toss. You filter, look, narrow, tag, then come
at the same table again with a different filter, the way a farmer tossed
the same heap over and over until it was clean.

[Filtering and searching](Filtering-and-searching.md) covers the filters,
[Tagging and notes](Tagging-and-notes.md) the tags, and
[Timeline, Search all and the Watchlist](Timeline-search-and-watchlist.md)
reading the tagged rows back across the case.

## Where you'll see it in Winnow

- **The launch animation.** Winnow opens by winnowing. Event IDs, ports and
  process names are thrown in from the left along with the grain, and in
  mid-air you can't tell them apart. The chaff is light enough for the wind
  to carry it off the right edge; the grain drops out of the stream and
  lands as the wordmark, as in the picture above. Any key or click skips
  it, and [Settings](Settings-and-shortcuts.md#launch-animation) can turn
  it off.
- **The tagline**, "separate the signal from the noise", under the
  wordmark on the loading screen and on the website's front page.
- **The Harvest skin**, the default: wheat gold for the accent, the color
  Winnow uses for whatever matters, against muted grays for everything
  else. ([Skins](Settings-and-shortcuts.md#skins))
- **The logo.** Its three bars get shorter from left to right and fade from
  gold to gray: grain kept, chaff going.
