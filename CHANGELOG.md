## [1.0.1](https://github.com/maxbolgarin/unread/compare/v1.0.0...v1.0.1) (2026-05-25)

### 📚 Documentation

* **readme:** fix wordmark image paths and polish copy ([c9c4df9](https://github.com/maxbolgarin/unread/commit/c9c4df9dc32a41a05f5582c276686bde0d75ec2e))
* **readme:** use absolute raw GitHub URLs for images so PyPI renders ([4b48d50](https://github.com/maxbolgarin/unread/commit/4b48d50dd8c5e7918be234031bc467604f2d7243))

## 1.0.0 (2026-05-25)

### 🚀 Features

* **dump:** accept local files / stdin (parity with analyze and ask) ([ac2cf42](https://github.com/maxbolgarin/unread/commit/ac2cf422ceb3155a45bccd8b6029c588735bd283))
* **unread:** add --no-console flag, rename content_language to report_language, and add source_language ([dd3034b](https://github.com/maxbolgarin/unread/commit/dd3034bbb762bbb1a18dfe6a0b2cbdfc93ec1af2))
* **tests:** add 6 test files and update locale semantics for three-axis language configuration ([fdc4dc9](https://github.com/maxbolgarin/unread/commit/fdc4dc92d0767b09b666e4c43d7de79525a22b84))
* **interactive:** add ALL_LOCAL sentinel + chat picker option ([78ea66c](https://github.com/maxbolgarin/unread/commit/78ea66cd8588cb429f8ea3507f6feed24a4115fa))
* **interactive:** add ask mode (chat → thread → period → confirm) ([a3a6a58](https://github.com/maxbolgarin/unread/commit/a3a6a58c49e486decd939a224a21df3c48644e0a))
* **config:** add ask.doc_full_text_cutoff_tokens (default 32000) ([6108fe4](https://github.com/maxbolgarin/unread/commit/6108fe4e22c23a732798fa4cc61082b3c049dea8))
* **site:** add astro marketing and docs site for github pages ([a5b7c77](https://github.com/maxbolgarin/unread/commit/a5b7c773acea3f6410676b77b0a9e4f4ce7b4f0f))
* **unread:** add atomic db transactions, path traversal guards, chunked file reads, and mp3 transcoding ([682922d](https://github.com/maxbolgarin/unread/commit/682922d12e5beb223f104932aabc0c3e634dd669))
* **install-bot:** add automated bootstrap script for linux environments ([a5ddd55](https://github.com/maxbolgarin/unread/commit/a5ddd55a424f142f62356736618f863a3368a41f))
* **deploy-bot:** add automated remote deployment script for docker-compose setup ([8bd15bd](https://github.com/maxbolgarin/unread/commit/8bd15bd3419859e4fdb40539c69a3ddd7c8eac93))
* **unread:** add bot runtime and progress tracking, and implement forward and window-based analysis ([7105158](https://github.com/maxbolgarin/unread/commit/710515847d7feb8ac0650cc868945271d5218f80))
* **assets:** add brand identity pack including icons, marks, social cards, and terminal banner ([ae7aeb6](https://github.com/maxbolgarin/unread/commit/ae7aeb60016e51b4046821ec030695aaf7797182))
* **unread:** add citation stripping, improve chunker diagnostics, and suppress status logs in silent mode ([afa8dbb](https://github.com/maxbolgarin/unread/commit/afa8dbb93c840c670656c9eda07af63e1039b77d))
* **unread:** add comment capping and preset listing, and improve forum read-marking logic ([046e2da](https://github.com/maxbolgarin/unread/commit/046e2da4dc91c05a62bf419d227df054c2339fbb))
* **enrich:** add comprehensive media enrichment pipeline with image, document, link and audio processing ([014fd92](https://github.com/maxbolgarin/unread/commit/014fd92d28fe15056d9925b0eadba208c9ae4c5e))
* **analyzer,interactive:** add comprehensive media enrichment with link processing, preset management, and enhanced cli with batch operations ([e6d1e54](https://github.com/maxbolgarin/unread/commit/e6d1e548e2a926e24509dd7159542b79b51e5b4f))
* **analyzer,export,tg:** add comprehensive message analysis, export and sync workflows ([824d012](https://github.com/maxbolgarin/unread/commit/824d012f82cc69b47e99aa3a21a86e219732f110))
* **unread:** add confirmation panel, burst message queuing, and batch execution logic ([2fe1bbd](https://github.com/maxbolgarin/unread/commit/2fe1bbd52a1a041da0eaefb861043d68e09e01ec))
* **security:** add credential storage backend abstraction with keystore support ([6c8db3d](https://github.com/maxbolgarin/unread/commit/6c8db3d391dc640204a5c8aac2abc249d9f1d8f6))
* add dump support for websites and youtube, implement update command, and add 5 test suites ([bbd4e1f](https://github.com/maxbolgarin/unread/commit/bbd4e1ff9579d4178865cbecd3f2263fbe401a88))
* **unread:** add dump support for websites and youtube, implement update command, and add 5 test suites ([4b78aa0](https://github.com/maxbolgarin/unread/commit/4b78aa0c2c2d538cca033a3544697cc9db1c7848))
* **ask:** add file / youtube / website source adapters ([cf37904](https://github.com/maxbolgarin/unread/commit/cf3790428d0bfab2ca8d1f976975f9a497c9a8fc))
* **install-bot:** add homebrew path detection and optimize system dependency installation ([e36ab82](https://github.com/maxbolgarin/unread/commit/e36ab8234c41787f5b34035a2d2352d043b59f8f))
* **ask:** add interactive chat retrieval and analysis with ask command ([d001066](https://github.com/maxbolgarin/unread/commit/d0010669a7f438897c5123102252b2ef5c71e189))
* **analyzer,export,tg:** add interactive mode, preset system, and comprehensive cli workflows ([0a50885](https://github.com/maxbolgarin/unread/commit/0a50885b9b09e5b4cefa6ab49d5c1bb9184089fc))
* **cli:** add linked discussion groups support with --with-comments flag and unified period defaults for --all-flat ([22a7c7d](https://github.com/maxbolgarin/unread/commit/22a7c7d4ebf2ffa60ecdd83354e5000cb637ce38))
* **unread:** add live model fetching, per-slot provider routing, and language utility module ([c81574a](https://github.com/maxbolgarin/unread/commit/c81574aa151d9dd7b68c1e7d54474abe7aed785c))
* **analyzer:** add max_chunk_input_tokens to cap request size below context window ([7ce4bac](https://github.com/maxbolgarin/unread/commit/7ce4bacdb4ec73decca58c5c20ed1014540ed38d))
* add models registry, redaction, completion, and session state tracking ([ce1397e](https://github.com/maxbolgarin/unread/commit/ce1397ef03afae77973b3e2064b8ecff354c274e))
* **i18n:** add multi-language support with preset localization and db-backed settings management ([34c36ed](https://github.com/maxbolgarin/unread/commit/34c36ed94f64198ccc7b45ee37ac9c4c40403d94))
* **unread:** add multi-turn chat to prompt, refactor report rendering, and add citation support ([293e1b9](https://github.com/maxbolgarin/unread/commit/293e1b966839f5ed3b5859d3106b3a5c1ddc8a70))
* **install-bot:** add non-interactive apt configuration to bootstrap script and update readme ([f533ec3](https://github.com/maxbolgarin/unread/commit/f533ec354f2e7dfe0e7725366522606fdd387b48))
* add optional dependency handling, interactive mode enhancements, and comprehensive test coverage with graceful degradation ([7d45492](https://github.com/maxbolgarin/unread/commit/7d454928c37a26d89d083edbb95f5c645b5f500f))
* **unread:** add PDF report generation with worker subprocess and improve bot startup session checks ([0effa46](https://github.com/maxbolgarin/unread/commit/0effa46cddb71611a958406a555eca3f3949cc4d))
* **core:** add prepare_chat_run and mark-read closure factory ([de782ee](https://github.com/maxbolgarin/unread/commit/de782ee6804a20458562b0cd465e240e5854c8d6))
* **core:** add prepare_chat_runs_per_topic and prepare_all_unread_runs iterators ([5205fb6](https://github.com/maxbolgarin/unread/commit/5205fb60fc006a17602d5d4a827001118dcb4cd2))
* **core:** add PreparedRun dataclass for chat-run pipeline handoff ([560eb92](https://github.com/maxbolgarin/unread/commit/560eb9243173ad02b99c1af4274c5048234ee6a4))
* add provider abstraction layer with anthropic and google support ([9dc2f63](https://github.com/maxbolgarin/unread/commit/9dc2f63fd17290e87424e707d076b9baeb6b40d8))
* **unread:** add report_format config option to toggle between pdf and md report output ([383c99d](https://github.com/maxbolgarin/unread/commit/383c99df0df9c43101ea8cf7249c0ddb011e10f1))
* **ask:** add run_interactive_ask + _resolve_ask_ref ([fb57ca5](https://github.com/maxbolgarin/unread/commit/fb57ca56cd3589771c5a75ff9664bf4f5e9e3034))
* **unread:** add self-hosted telegram bot frontend with configuration, command surface, and i18n support ([6e64b5e](https://github.com/maxbolgarin/unread/commit/6e64b5ec42de6a045c14de59c6a948b4b3244c5b))
* **analyzer,tg:** add single message analysis with link parsing and preset defaults ([fcacb3a](https://github.com/maxbolgarin/unread/commit/fcacb3a3b10def32b7541435cfcc5bc580b51da5))
* **unread:** add telegram bot app with dispatcher, command handlers, and session upload support ([c2fa468](https://github.com/maxbolgarin/unread/commit/c2fa468db8ac875f8859c106e64a5bd47e3b3824))
* **site:** add telegram bot section, documentation page, and faq entries ([17460ee](https://github.com/maxbolgarin/unread/commit/17460ee1e4425fbf44b1099653db550287b03fd7))
* add telegram message analyzer with openai integration and local caching ([2becf05](https://github.com/maxbolgarin/unread/commit/2becf05e54e75a0b5fb697540555749eb31a633d))
* **analyzer:** add token budget validation with descriptive error messages ([af42a95](https://github.com/maxbolgarin/unread/commit/af42a95edcff90e6180b6ac7c13c7b67412e9aa8))
* add weasyprint support for PDF reports and update dockerfile and readme ([1b9a462](https://github.com/maxbolgarin/unread/commit/1b9a462157c17387f1b94160b2d6a6d7c78aefbb))
* **ask:** add wizard, positional ref, global scope, enrich, mark-read, and improved UX ([c65ffcf](https://github.com/maxbolgarin/unread/commit/c65ffcfde7ed3130945fa88b424134a1b8412d75))
* **youtube:** add YouTube video analysis with captions and Whisper fallback ([ab0f877](https://github.com/maxbolgarin/unread/commit/ab0f877cdb14c556575cc2fb54bf08471a396776))
* **crypto:** auto-migrate v1 AEAD ciphertexts to v2 with slot-bound AAD ([05c14d3](https://github.com/maxbolgarin/unread/commit/05c14d3701cb30d21890e0093d7d82b2e31682a9))
* **install-bot:** automate unread init and add recovery logic for interactive setup ([f44b6ca](https://github.com/maxbolgarin/unread/commit/f44b6caeffef24d461f3a6ddc473fdfa09336cb1))
* **install-bot:** bump version to 0.2.0 and refactor installation script to manual init flow ([a2ccf62](https://github.com/maxbolgarin/unread/commit/a2ccf62551c5521fd6ebddd680e6881879dff9d4))
* **analyzer,export:** enhance command options with console output, read status tracking and improved defaults ([635f2c9](https://github.com/maxbolgarin/unread/commit/635f2c900c183417df2593e53e59f2c931b9a5c0))
* **cli:** expand ask command with embeddings, reranking, and interactive retrieval plus comprehensive documentation updates ([cb3bf75](https://github.com/maxbolgarin/unread/commit/cb3bf7579cf0e722d0a76dc2c8929b34589f20ab))
* **enrich:** make link enrichment opt-in and update documentation and config defaults ([b18b91f](https://github.com/maxbolgarin/unread/commit/b18b91f390c5a744bc61ee573aaf880f1a420096))
* **unread:** move ffmpeg preflight check and bump version to 0.1.1 ([845134a](https://github.com/maxbolgarin/unread/commit/845134a58dc753c46a07b1aeedb362a9b29e8e67))
* **cli:** move folders under describe; drop top-level folders command ([19c7184](https://github.com/maxbolgarin/unread/commit/19c7184d2a6e1c8def91ad17a92794e8e2af36df))
* move telegram commands under tg subgroup, update docs, and bump version to 0.1.1 ([9d135c7](https://github.com/maxbolgarin/unread/commit/9d135c72786303ac732d3ebdf387a6a7f4012985))
* **unread:** overhaul interactive wizard, add multi-turn chat, and refine provider routing logic ([f31ff77](https://github.com/maxbolgarin/unread/commit/f31ff776b3d2727352590ff7e27dbef5f8f905cc))
* **unread:** overhaul interactive wizard, add OpenRouter headers, and refine citation logic ([2cb7aca](https://github.com/maxbolgarin/unread/commit/2cb7acafc2d865eb8ee304326a18fc95ec30e0a5))
* **ai:** per-model truncation cap, reasoning-temp guard, Gemini safety + assert ([46300e8](https://github.com/maxbolgarin/unread/commit/46300e8140f4388cb0d996a4b7f9973e79c3cc4c))
* **ask:** pre-dispatch URL/file/stdin refs to source adapters before TG ([a23fda3](https://github.com/maxbolgarin/unread/commit/a23fda3140c6ffcf1747fe3050fe50dbc1664bda))
* **logging:** production-ready RotatingFileHandler + Settings.logging.file_path ([2dee21b](https://github.com/maxbolgarin/unread/commit/2dee21bc72eacfd2a6f5ed9290dcdeade69a5699))
* **tokens:** provider-aware safety margin for Claude / Gemini counts ([26efafe](https://github.com/maxbolgarin/unread/commit/26efafe68009277515f5feb31d45a5caa6c102df))
* **cli:** rename Sync panel to Telegram and regroup telegram commands ([843b194](https://github.com/maxbolgarin/unread/commit/843b194435d501734db1d6e43c67e3d7d8fcff0d))
* **core:** scaffold analyzetg.core package ([b54cc53](https://github.com/maxbolgarin/unread/commit/b54cc537c98a32fc95e284e8a6063a02872a65a0))
* **ask:** scaffold sources package + cmd_ask_document orchestrator ([1ced1c7](https://github.com/maxbolgarin/unread/commit/1ced1c7fc00ab9f193151f4cd242d978747e128b))
* **install-bot:** switch to uv for dependency management and add libpango as a system requirement ([b9cc226](https://github.com/maxbolgarin/unread/commit/b9cc22608eeda00a330b01f39e5d9e70af08702c))
* **cli:** tab-complete local file paths for the <ref> positional ([66942e6](https://github.com/maxbolgarin/unread/commit/66942e6dafd2e57eb24dd72bb7a318d83561c5d1))
* **dump:** thread full enrichment pipeline through dump + wizard + structured exports ([327782f](https://github.com/maxbolgarin/unread/commit/327782f0292c192bdabc144731da9eb0801302a1))
* **site:** update to astro 6, refresh branding assets, and refactor components ([9ead6b9](https://github.com/maxbolgarin/unread/commit/9ead6b923aa58ad3e1bd5f4daf35d983e502a025))
* **presets:** update website prompt to v2 and remove paragraph citation requirements ([dbcc33c](https://github.com/maxbolgarin/unread/commit/dbcc33c94da324f4ff51e2859a0badbaec634601))
* **prompts:** wrap untrusted content with sentinels; chunker splits oversized ([fb5dff1](https://github.com/maxbolgarin/unread/commit/fb5dff1cea7e0bd580ff09d5b26588ef31d1dbe8))

### 🐛 Bug Fixes

* **logging:** bump redactor recursion depth from 2 to 6 ([c4c528d](https://github.com/maxbolgarin/unread/commit/c4c528d41a4fabbc1765c60add3ec95ce5770cc1))
* **media:** centralize file-size cap inside download_message ([78920fd](https://github.com/maxbolgarin/unread/commit/78920fde40225d7daf3eeb57e4883c5a30a03fae))
* **cli:** delegate path completion to the shell — no trailing space ([22cf1b1](https://github.com/maxbolgarin/unread/commit/22cf1b16ee945c639e3c9e5d39bbfc594383cb74))
* **tokens:** graceful fallback when tiktoken can't reach its blob ([a0da2b8](https://github.com/maxbolgarin/unread/commit/a0da2b8a116533495331b8068e778f08a9c48e95))
* **config:** isolate .env values from os.environ; subprocess inheritance closed ([23b2e68](https://github.com/maxbolgarin/unread/commit/23b2e68b1cc927dc369ecef87f2c285b93647a6f))
* **secrets:** namespace keyring service per install ([96ffe37](https://github.com/maxbolgarin/unread/commit/96ffe3704c43651b8f4b65b03cd6768368157857))
* pre-prod batch 2 — provider robustness, sync hardening, doc drift ([6ff9dd5](https://github.com/maxbolgarin/unread/commit/6ff9dd512b1ed1893049154fe781f0a16bcf2716))
* pre-prod batch 3 — crypto hardening, robustness, error visibility ([69a4d80](https://github.com/maxbolgarin/unread/commit/69a4d80f52c39c25ae774564ae0646cffc79d822))
* pre-prod batch 4 — AEAD slot binding, retry parity, hardening ([1495da3](https://github.com/maxbolgarin/unread/commit/1495da381988967cfbd523791729406e539855eb))
* pre-prod blockers — killme path safety, .env hardening, filename sanitization ([419bc2c](https://github.com/maxbolgarin/unread/commit/419bc2c327cb5ca2621597d887724b3669c01e76))
* **dump:** preserve original file extension; copy bytes instead of extracting ([6c8c8b1](https://github.com/maxbolgarin/unread/commit/6c8c8b14fe725a80bf68a46d1afe716d3065a39d))
* **install-bot:** reattach stdin to tty to enable interactive prompts during setup ([8479182](https://github.com/maxbolgarin/unread/commit/8479182eb92037c3adae2c5aa45f1538f6b2c956))
* **media:** restore download-media for media-only messages ([891a153](https://github.com/maxbolgarin/unread/commit/891a153b1cfdbe1756703c04d0d1cb50d3f3c0b4))
* **install-bot:** sanitize bot token input to strip carriage returns and whitespace ([e33d752](https://github.com/maxbolgarin/unread/commit/e33d75210ca5da057e7ef85ad79596042a5b8553))
* **install:** seed config.toml.example via secret_write_text ([c15886d](https://github.com/maxbolgarin/unread/commit/c15886d927fa02ba46a0ca33597a08a7f5e15497))
* **chunker:** split or truncate oversized single messages instead of failing the call ([25fb842](https://github.com/maxbolgarin/unread/commit/25fb84278d2302e96f31e03740361f11aaf73d9e)), closes [#msg_id](https://github.com/maxbolgarin/unread/issues/msg_id)
* **ask:** stdin auto-route + scope-flag rejection on doc refs ([055b81c](https://github.com/maxbolgarin/unread/commit/055b81cbf1eaf25c98db7f2b0fe90318639869b9))
* **cli:** unblock event loop in watch by using asyncio.create_subprocess_exec ([4532c2a](https://github.com/maxbolgarin/unread/commit/4532c2a1dabd5bbce76aebe573dbde102cc6308d))
* **deploy-bot:** update default compose file path to docker-compose.bot.yml ([292ca13](https://github.com/maxbolgarin/unread/commit/292ca13d8dda7555b7c7b60175819e4ab4107232))
* **interactive:** use UTC-aware datetimes for custom date range count ([14256a7](https://github.com/maxbolgarin/unread/commit/14256a79e16fc15d767a56b0ce5dd4cc36215bed))
* **ask:** wire content_language, enforce max_cost, rename helper ([4db56c7](https://github.com/maxbolgarin/unread/commit/4db56c7ced58a7f5236398be3a529ca80f415512))

### ⚡ Performance Improvements

* **db:** stream iter_messages / untranscribed_media / cache_iter_full ([5496191](https://github.com/maxbolgarin/unread/commit/5496191311dd26dec6afa653cfe7700219aaa386))

### 📚 Documentation

* add bot documentation and update deployment and media enrichment guides ([8caf0ee](https://github.com/maxbolgarin/unread/commit/8caf0eebf7013c6c86e4a99398231fc17dfcab88))
* **analyze-example:** add demonstration gif for analysis workflow ([b425e9a](https://github.com/maxbolgarin/unread/commit/b425e9a42e4d4682ceb5e959bca8d2221649a16a))
* add documentation for configuration, installation, reference, security, and sources ([939f5e4](https://github.com/maxbolgarin/unread/commit/939f5e4360def62fbc9cba991b8c08f7ed489827))
* **bot:** add example bot image to documentation ([51ceec7](https://github.com/maxbolgarin/unread/commit/51ceec7cb273b6256e15b19e3c8b5c3ad481ad1f))
* **summary:** add example output for analysis workflow ([11ccb12](https://github.com/maxbolgarin/unread/commit/11ccb122df8d51929ba58eca87e1b37b2ef58dfb))
* add guide for deploying unread-bot to a remote linux vm ([f16359d](https://github.com/maxbolgarin/unread/commit/f16359dd4023df4024d0f5d8f84d22fe6cb7e245))
* **security:** add vulnerability reporting policy and supported versions ([01fe923](https://github.com/maxbolgarin/unread/commit/01fe9231f86d31d0ea3cf9cf9cae2edff8aaef95))
* align CLAUDE.md, README, skill with current CLI panel layout ([e09e15c](https://github.com/maxbolgarin/unread/commit/e09e15c793de5a608a88359160ac1f26529fc8f1))
* **presets:** bump tldr prompt version and increase token budgets in en and ru locales ([943ad44](https://github.com/maxbolgarin/unread/commit/943ad4433b4ca7b1a0f5310f11e84cf12a02bad4))
* **bot-vm-deploy:** clarify stdin handling and troubleshooting steps for unread init ([dcd1f9b](https://github.com/maxbolgarin/unread/commit/dcd1f9bf9b18ed924503def34fe9e8368be190b3))
* **cli:** document why describe_app needs _UnreadRootGroup ([678a907](https://github.com/maxbolgarin/unread/commit/678a9074ff79c06bdb51c4d9d95b634387a72867))
* **_base:** enforce output language consistency in english and russian presets ([0b46b60](https://github.com/maxbolgarin/unread/commit/0b46b6039b016e266ab17d34593b250fcb8c14f3))
* **readme:** improve shell alias setup with zsh function and bash examples ([0328963](https://github.com/maxbolgarin/unread/commit/032896311ca444fe32c4c8514d18385e749ec69a))
* move assets to docs directory and update readme with usage examples ([ae3067b](https://github.com/maxbolgarin/unread/commit/ae3067b8513a6a79580255b1315102a5fbc80d0b))
* **code-review:** narrow to post-v1.0 backlog only ([31af4ec](https://github.com/maxbolgarin/unread/commit/31af4ec068f368e96929324506aa9b2070b80861))
* pre-prod code review — 11 blockers and findings ([1218cde](https://github.com/maxbolgarin/unread/commit/1218cde7b44b34061120e32bd9ebe009838db3b9))
* **readme:** reflect Telegram panel + describe folders + ref-shape parity ([2f1c51a](https://github.com/maxbolgarin/unread/commit/2f1c51aef69a21ffa1cf0e193723127d567c2c3e))
* rewrite bot deployment guide to include local uv install and updated native/docker paths ([1d69a68](https://github.com/maxbolgarin/unread/commit/1d69a682a47ac5c9cd9a98a125baeb71ca2c9168))
* rewrite readme to simplify installation and usage instructions ([e035543](https://github.com/maxbolgarin/unread/commit/e0355430c911a818975fdb1f0144a74a01abcdf0))
* **presets:** rewrite readme, remove broad preset, and add tldr preset in en and ru locales ([6fe0e13](https://github.com/maxbolgarin/unread/commit/6fe0e13cbf4d0e14ffbd8e1c48984b1a33bcb1cc))
* spec for ask wizard parity, ref auto-resolve, follow-up prompt ([5c7aabf](https://github.com/maxbolgarin/unread/commit/5c7aabf67e79df1105fafe9dd4c2a305e47d1bf6))
* **readme:** tighter intro, 60-second quickstart, professional polish ([a2349ae](https://github.com/maxbolgarin/unread/commit/a2349ae3dad52682bd9081667f0e5caba55d6a82))
* **readme:** translate to english and restructure with improved examples ([e844ad8](https://github.com/maxbolgarin/unread/commit/e844ad8fe152fc640049c1ba2cad894cff7e94a9))
* update locale configuration and cache management documentation ([85e6444](https://github.com/maxbolgarin/unread/commit/85e6444d8fbc646151c376807b1b3338f84059b9))
* **presets:** update multichat command syntax in readme and bump prompt version in ru locale ([ddf1a73](https://github.com/maxbolgarin/unread/commit/ddf1a7327c3c0c72a54e170aff209f01263a3881))
* update preset documentation in readme with new tldr, highlights, video, and website options ([32fef41](https://github.com/maxbolgarin/unread/commit/32fef4196b596340641ed4c774be896eb3e60ae9))
* update readme with centered bot example image formatting ([09e65a3](https://github.com/maxbolgarin/unread/commit/09e65a3fc300677301268751792ca29514103a30))
* update readme with media enrichment details and bot deployment documentation ([2cd50ca](https://github.com/maxbolgarin/unread/commit/2cd50ca7eb8dfd8064f018d46dbc8bd2564a2f7f))

### 📦 Code Refactoring

* consolidate docker images into single generic runtime and update bot documentation ([52c39d7](https://github.com/maxbolgarin/unread/commit/52c39d7eddaf1e3d70db5c7ff29d881eff148035))
* **cli:** convert describe from leaf command to Typer group ([83b8db0](https://github.com/maxbolgarin/unread/commit/83b8db078b1f47c4b478afb03d10e5ae6195059a))
* **cli:** drop redundant invoke_without_command on describe app ctor ([72d4900](https://github.com/maxbolgarin/unread/commit/72d4900f926f9022087558a709cbfc9c0a5ffd00))
* **ask:** drop redundant lazy import; tighten TODO marker ([29b0687](https://github.com/maxbolgarin/unread/commit/29b06870b3fc0fe4326e98a575db37aed92e7923))
* extract i18n strings and consolidate error/banner messaging ([321e586](https://github.com/maxbolgarin/unread/commit/321e5860b90c4c31508cbef3d296ea7b450d9a8f))
* **media:** improve error handling with better ffmpeg stderr reporting and media type context ([21359b2](https://github.com/maxbolgarin/unread/commit/21359b227dfc4365749bd75fbbc65c8f6195fdb9))
* **analyze:** migrate cmd_analyze to prepare_chat_run pipeline ([21044ee](https://github.com/maxbolgarin/unread/commit/21044eee13d2010dab39074f7355d9cddd38ae50))
* **dump:** migrate cmd_dump to prepare_chat_run pipeline ([356a3ab](https://github.com/maxbolgarin/unread/commit/356a3ab30238b123ac927ea417d18e6f5eb10113))
* **site:** migrate to tailwindcss/postcss and remove unused vite plugin ([93a7abc](https://github.com/maxbolgarin/unread/commit/93a7abc89797928ee3ac3411dd692030ff897930))
* **assets:** remove unused os import in render script ([36ded9b](https://github.com/maxbolgarin/unread/commit/36ded9b4a008f6195ef889553678d421b4fcecfe))
* rename analyzetg package to atg throughout codebase ([27e5822](https://github.com/maxbolgarin/unread/commit/27e5822a2475ef4644bb9a95b224945cdef173ec))
* rename atg package to unread throughout codebase ([703f1b4](https://github.com/maxbolgarin/unread/commit/703f1b426ecd47b155b802cb3c0a10ed9618d3c2))
* reorganize core pipeline, db schema, and enrichment architecture with comprehensive test coverage ([4ea3e0f](https://github.com/maxbolgarin/unread/commit/4ea3e0f9cb99bc8c3a388e1b30175240f4855ee7))
* **cli:** reorganize help output and add common patterns guide ([54da54b](https://github.com/maxbolgarin/unread/commit/54da54b8f25a05b92787c561c20ccff74ac89ab6))
* **unread:** restrict setup prompt to first-run only ([2544c81](https://github.com/maxbolgarin/unread/commit/2544c81bce9741893dfb3a1b5ddc36de531ee55c))
* **analyze:** run_analysis accepts pre-prepared messages ([b4847b0](https://github.com/maxbolgarin/unread/commit/b4847b0c14ba3e30b15b4f4b81c11e99e3ebc1cd))
* **media:** unify download-media with save_raw_media helper ([36107c7](https://github.com/maxbolgarin/unread/commit/36107c7ae1039961b13f2bea3d73c1ffd6e5e16d))

### 🚨 Tests

* add 10 test files and update 5 existing tests for language configuration and wizard logic ([fb741c7](https://github.com/maxbolgarin/unread/commit/fb741c7b05bd0f35088fdcc05a9a05cbd360a15a))
* add 2 citation tests and update 3 existing test suites for prompt interaction and rendering ([a134fcc](https://github.com/maxbolgarin/unread/commit/a134fccbaa722cd2e904e9e2cb94ed2917217c45))
* add 4 test suites for bot runtime, replies, progress, and forum pick mode and expand confirm tests ([db77c72](https://github.com/maxbolgarin/unread/commit/db77c7283f6ede82746ea715f414689999ae8a06))
* add 5 test files for interactive wizard logic, citation rendering, and topic counts ([2dc56bc](https://github.com/maxbolgarin/unread/commit/2dc56bc435b222d5e3f15eda322fa043f9c3ce2b))
* add backfill cache tests, update cli error handling, and refine markdown export tests ([96d56c0](https://github.com/maxbolgarin/unread/commit/96d56c01c9aabf159cc41f5e35d5859aec3d1e7c))
* add comprehensive test suite for settings, credentials, permissions, and help ([462812b](https://github.com/maxbolgarin/unread/commit/462812b3412bf4abfcba8ff7528e86332467000b))
* add image skip regression tests and interactive wizard preset loop tests ([8bbe862](https://github.com/maxbolgarin/unread/commit/8bbe862bf093f37f25056677a550f2179a8a133e))
* add logout command test, update help and provider routing tests, and refine base version checks ([25485aa](https://github.com/maxbolgarin/unread/commit/25485aa74dba42fd03b70d8171680f1a625c2bb4))
* **credential_gating:** add regression test for skipping setup prompt ([1aeafad](https://github.com/maxbolgarin/unread/commit/1aeafadd013573e71272040b8b058c6b9eae8e25))
* add tldr extraction tests and expand bot config, dispatcher, and session upload test coverage ([e4297cd](https://github.com/maxbolgarin/unread/commit/e4297cdb8d8326f1f1cc2ff4b9b841f5b627847f))
* **bot:** add unit tests for bot configuration, command dispatching, and session handling ([31d5241](https://github.com/maxbolgarin/unread/commit/31d52414d0cc558acd045ef626b3922cf4a437bd))
* add unit tests for bot confirmation panel logic and callback handling ([87a2c80](https://github.com/maxbolgarin/unread/commit/87a2c80996edaa3a32f9782b3dcebbfc2914f30e))
* add v3 AEAD envelope support, path traversal guards, and concurrency tests ([4743644](https://github.com/maxbolgarin/unread/commit/4743644f5d10317aba01551cfa139f8b756a2205))
* add validation and default tests for bot report format configuration ([118f7ad](https://github.com/maxbolgarin/unread/commit/118f7adc9230383bf3916636a980f1ab0f9117b4))
* catch up async-iterator + sentinel-format fixtures ([9361493](https://github.com/maxbolgarin/unread/commit/93614937064c55ef003b09540e7b2145547de447))
* cover completion, export, core paths packages ([dc46014](https://github.com/maxbolgarin/unread/commit/dc46014f479a082e8411324759d00ac481ef9a99))
* **ask:** pin doc-ref question-prompt TTY/non-TTY behavior ([4cd955f](https://github.com/maxbolgarin/unread/commit/4cd955ff81242717071f4186971881545350708d))
* **tg:** pin iter_messages kwargs across (forward, since_date, from_msg_id) ([502504a](https://github.com/maxbolgarin/unread/commit/502504a88a352b45a3dcb9ca8e1e9e3fa9a68dab))
* **panel:** pin panel header line, not bare 'Telegram' substring ([be297ce](https://github.com/maxbolgarin/unread/commit/be297ce11be278f8b28c4ec9b9e76ed662cba21d))
* **ai:** real-translation tests for Anthropic and Google adapters ([5d9d0e7](https://github.com/maxbolgarin/unread/commit/5d9d0e7126393332dc63a2cc8b0a6a7a70d856c6))
* **e2e:** smoke tests for version, --help, doctor, file-routing wiring ([0182092](https://github.com/maxbolgarin/unread/commit/0182092c955480f2739ece3991775c721e452f52))
* **core:** smoke-test prepare_chat_run end-to-end ([aec3a00](https://github.com/maxbolgarin/unread/commit/aec3a004d7370635227dc78a7cbc5dd189c46b14))
* strip ansi codes in cli tests and fix xdg runtime path in killme test ([ff8e36f](https://github.com/maxbolgarin/unread/commit/ff8e36fedda0b14c5dd50d5a83b4a50a2b1335d3))
* **flood:** stub asyncio.sleep in final attempt test ([8ef711c](https://github.com/maxbolgarin/unread/commit/8ef711c41ccdae71dcdeb4e8e6a8264822d0719e))
* **cli:** update help layout tests to verify status panel and ref table ([8e3c40a](https://github.com/maxbolgarin/unread/commit/8e3c40a8f62d1f43177304d8b7ef1a2804c83518))

### 🛠 Build System

* **uv:** add lock file for reproducible builds ([12dacbf](https://github.com/maxbolgarin/unread/commit/12dacbfeb092871e91f43da0eea49e6094dd4a0e))
* **release:** bump setup-uv to v4 in release workflow ([061b2dd](https://github.com/maxbolgarin/unread/commit/061b2dd1011ca8bc17e128e7152e8760669193ae))

### ⚙️ Continuous Integration

* add docker image build and push workflow for ghcr ([66405e8](https://github.com/maxbolgarin/unread/commit/66405e818923cdac5968c6205dd5a7d5dc1eb44a))
* **.github:** add docker image build and push workflow for unread-bot ([bd2057f](https://github.com/maxbolgarin/unread/commit/bd2057f7246e3f374f9b7dd772393521ace3aec7))
* add github actions workflow with lint and test jobs ([44f0bce](https://github.com/maxbolgarin/unread/commit/44f0bce4ebec86c1d3abb4bdd44038335a2ebc1d))
* **.github:** add issue and PR templates, refactor release workflow ([b0e811f](https://github.com/maxbolgarin/unread/commit/b0e811f3a27e6bcd861656fbe59c2dd70874e579))
* **release:** add semantic-release workflow with github actions for automated versioning and publishing ([71420b1](https://github.com/maxbolgarin/unread/commit/71420b127d7451f6fe3274311379f7434580c1de))
* **.github:** install ffmpeg and pin python version for pytest in ci and release workflows ([da37cec](https://github.com/maxbolgarin/unread/commit/da37cec37c06fdaa2f0161e44302096a14c9b75c))

### 🔧 Chores

* add docker deployment support and update license to apache 2.0 ([87ab2a6](https://github.com/maxbolgarin/unread/commit/87ab2a650672bfa6805637c6ffd5a4650d826dd8))
* add production docker-compose configuration for unread-bot ([d055095](https://github.com/maxbolgarin/unread/commit/d055095f023c8ac8298b71ffba06220fad97a279))
* add python version pin, contributing guide, changelog template ([c0a8889](https://github.com/maxbolgarin/unread/commit/c0a88899e86d7077d88dca1cf4b47cb4e0123187))
* **misc:** bump unread version to 0.2.0 in uv.lock ([242b12c](https://github.com/maxbolgarin/unread/commit/242b12cb592de6777db8b4e1c11a928a2811ad75))
* **format:** collapse nested with into single statement (SIM117) ([5770ff8](https://github.com/maxbolgarin/unread/commit/5770ff8f3a45dd2ca9e316fbadb45e7d8a32d116))
* consolidate slug/path helpers into analyzetg.core.paths ([86a7866](https://github.com/maxbolgarin/unread/commit/86a786682fca1526782fcd143d34624adb13dbd7))
* **release:** mark 1.0.0 production/stable ([1c3dd1e](https://github.com/maxbolgarin/unread/commit/1c3dd1e44b4d090748aa85745d08461d53447902))
* remove legacy config path env var aliases ([6b6aa58](https://github.com/maxbolgarin/unread/commit/6b6aa5831209f3d0d133dc593004e979f7ddce16))
* remove obsolete CODE_REVIEW.md documentation ([83fdd96](https://github.com/maxbolgarin/unread/commit/83fdd9652a60a0e0ee7446a30c81e35ac135547b))
* remove unused gitkeep files from reports and storage directories ([78e20aa](https://github.com/maxbolgarin/unread/commit/78e20aa546fe67b48d06138bd278369d764f26c9))
* **format:** ruff format pass on new panel test file ([c5ef0d9](https://github.com/maxbolgarin/unread/commit/c5ef0d904d59ace125f0ec03a942a9e02a8c030b))
* silence E402 noqa in conftest, format catchup, openai_client docstring ([f4e9039](https://github.com/maxbolgarin/unread/commit/f4e90393dcb07f35b13f9df48d4df14965b95f78))
* untrack storage/ data and tighten .gitignore ([8e8f803](https://github.com/maxbolgarin/unread/commit/8e8f803950c6c001aab4c5b484311737b83044c5))

# Changelog

All notable changes to this project will be documented in this file.

This file is maintained automatically by [semantic-release](https://semantic-release.gitbook.io/)
based on [Conventional Commits](https://www.conventionalcommits.org).

## 0.1.1

### Breaking

- **Telegram setup / inspection verbs moved under `unread tg`.** Top-level
  `unread login`, `unread logout`, `unread sync`, `unread chats add/list/…`,
  and `unread describe folders` no longer resolve. Use the subgroup form:
  - `unread login` → `unread tg login`
  - `unread logout` → `unread tg logout`
  - `unread sync` → `unread tg sync`
  - `unread chats add` → `unread tg chats add`
  - `unread chats list/enable/disable/remove` → `unread tg chats manage`
    (consolidated into one interactive panel)
  - `unread chats run` → `unread tg chats run`
  - `unread describe folders` → `unread tg describe folders`

  The motivation is source extensibility (a future WhatsApp / Slack source
  would mirror this as `unread wa describe`, etc.). Shell aliases / scripts
  using the old top-level verbs will need to be updated.

### Changed

- Interactive wizard overhaul: multi-turn chat support, refined provider
  routing (OpenAI / OpenRouter / Anthropic / Google / Local), citation
  rendering refinements, and consolidated subscription management into
  `unread tg chats manage`.
- New presets: `tldr` (en + ru) — two-or-three-sentence phone-screen scan.
- Removed preset: `broad` (en + ru) — superseded by `summary` / `digest`.
  Custom configs referencing `--preset broad` will need to be updated.
- All `doctor` / banner / i18n strings updated to reference the new
  `unread tg <verb>` spellings (previously pointed users at commands that
  did not exist after the subgroup move).
- `presets/ru/multichat.md` `prompt_version` bumped `v1` → `v2` (dropped a
  stale command-name reference in the system prompt; cache rows for this
  preset get re-keyed on upgrade).
