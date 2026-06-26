# Source Inventory

Verified on 2026-06-26.

## Summary

- Xiaoyuzhou: 13 valid podcast homepages.
- YouTube: 28 valid sources, including 10 channel video tabs and 18 playlists.
- No invalid source was found in the cleaned URL list during this validation pass.

## YouTube Crawlability

YouTube sources can be expanded with `yt-dlp`.

Channel video tabs can be paged through to collect videos. A quick flat scan gives each video's ID, URL, title, duration, uploader/channel, and often live/premiere flags. A detail pass can add upload date, description, view/like/comment counts, chapters, categories, tags, thumbnail, and available manual/automatic subtitle languages.

Playlists can also be expanded, and `yt-dlp` often returns `playlist_count`. Current validated playlist counts include:

- The OpenAI Podcast: 23
- Dwarkesh Podcast: 134
- Bloomberg Surveillance Full Shows: 619
- Bloomberg The China Show: 1043
- The Daily: 912
- Odd Lots Audio: 851

Configured duration filters:

- Interesting Times with Ross Douthat: ignore videos shorter than 10 minutes.
- Lex Fridman: ignore videos shorter than 30 minutes.

## Xiaoyuzhou Crawlability

Xiaoyuzhou podcast homepages are publicly readable. The public page embeds:

- Podcast: pid, title, author, brief, description, subscription count, image, color, status, episode count, podcasters, contacts, pay type, latest episode publication date.
- Recent episodes: the first 15 episodes only, including eid, title, description, duration, pubDate, media/audio URL, media size/type, play/clap/comment/favorite counts, pay type, transcriptMediaId when present, and episode permissions.

Important limitation: public podcast pages do not expose all historical episodes. Full pagination/list APIs and official transcript APIs are behind Xiaoyuzhou/Jike authentication. With `XIAOYUZHOU_ACCESS_TOKEN`, the same project can be extended to request authenticated episode lists and official transcript sentences. Without it, the reliable public route is: latest 15 from page, individual episode metadata from episode page, and audio-based ASR fallback.
