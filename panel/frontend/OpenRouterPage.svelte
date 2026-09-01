<script lang="ts">
	// Lives entirely outside llama.cpp's own source tree — see ../README.md.
	// Reached from the "Browse OpenRouter models" link in RightBar.svelte.
	// The route file that makes this discoverable to SvelteKit's router is
	// tools/ui/src/routes/openrouter/+page.svelte — a 3-line stub inside the
	// fork that imports and renders this file, the same pattern RightBar uses
	// to keep the actual UI code out of llama.cpp's tree. A route file has to
	// physically exist under src/routes for SvelteKit to discover it at all;
	// this is the smallest that requirement can be made.
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import * as Table from '$lib/components/ui/table';
	import { onMount } from 'svelte';

	const MODELS_URL = 'http://127.0.0.1:9010/api/openrouter/models';
	const PINNED_URL = 'http://127.0.0.1:9010/api/openrouter/pinned';
	const PIN_URL = 'http://127.0.0.1:9010/api/openrouter/pin';
	const UNPIN_URL = 'http://127.0.0.1:9010/api/openrouter/unpin';

	type OpenRouterModel = {
		id: string;
		name: string;
		context_length: number;
		pricing: { prompt: string; completion: string };
		architecture?: { input_modalities?: string[] };
	};

	let models = $state<OpenRouterModel[] | null>(null);
	let loadError = $state('');
	let query = $state('');
	let copiedId = $state('');
	let sortKey = $state<'name' | 'context' | 'price'>('name');
	let showFavoritesOnly = $state(false);

	// Favorites live in this browser's localStorage, not the panel backend —
	// same reasoning as everything else scoped per-browser in this setup
	// (the card-based MCP servers work the same way): this is a personal
	// shortlist, not shared state, so there is nothing to keep in sync
	// server-side and no reason to add a place for it to go stale.
	const FAVORITES_KEY = 'openrouter-favorite-models';
	let favorites = $state<Set<string>>(new Set());

	function loadFavorites() {
		try {
			const raw = localStorage.getItem(FAVORITES_KEY);
			favorites = new Set(raw ? JSON.parse(raw) : []);
		} catch {
			favorites = new Set();
		}
	}

	function toggleFavorite(id: string, e?: Event) {
		e?.stopPropagation();
		const next = new Set(favorites);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		favorites = next;
		localStorage.setItem(FAVORITES_KEY, JSON.stringify([...next]));
	}

	async function load() {
		try {
			const res = await fetch(MODELS_URL, { signal: AbortSignal.timeout(20000) });
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const body = await res.json();
			models = body.data ?? [];
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load';
		}
	}

	// Pinning is different from favoriting above: a favorite is personal,
	// per-browser localStorage. A pin is global — it registers the model as
	// a real, selectable entry in llama.cpp's own normal chat model dropdown
	// for anyone hitting this one llama-server (see the
	// SERVER_MODEL_SOURCE_REMOTE handling in server-models.cpp). Kept as a
	// visibly separate action so starring a model on one device can't
	// silently change what shows up in chat on another.
	let pinnedIds = $state<Set<string>>(new Set());
	let pinBusy = $state<Record<string, boolean>>({});
	let pinError = $state('');

	async function loadPinned() {
		try {
			const res = await fetch(PINNED_URL, { signal: AbortSignal.timeout(6000) });
			const body: { id: string }[] = await res.json();
			pinnedIds = new Set(body.map((e) => e.id));
		} catch {
			// pin buttons just show as unpinned until the backend is reachable
		}
	}

	async function togglePin(model: OpenRouterModel, e?: Event) {
		e?.stopPropagation();
		const wasPinned = pinnedIds.has(model.id);
		pinBusy = { ...pinBusy, [model.id]: true };
		pinError = '';
		try {
			const res = await fetch(wasPinned ? UNPIN_URL : PIN_URL, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ id: model.id, name: model.name }),
				signal: AbortSignal.timeout(10000)
			});
			if (!res.ok) throw new Error((await res.json().catch(() => null))?.error ?? res.statusText);

			const next = new Set(pinnedIds);
			if (wasPinned) next.delete(model.id);
			else next.add(model.id);
			pinnedIds = next;
		} catch (err) {
			pinError = err instanceof Error ? err.message : 'Failed to update pin';
		} finally {
			pinBusy = { ...pinBusy, [model.id]: false };
		}
	}

	onMount(() => {
		void load();
		loadFavorites();
		void loadPinned();
	});

	function perMillion(pricePerToken: string): string {
		const v = Number(pricePerToken) * 1_000_000;
		if (!Number.isFinite(v)) return '—';
		if (v === 0) return 'free';
		return `$${v < 0.01 ? v.toFixed(4) : v.toFixed(2)}`;
	}

	async function copyId(id: string) {
		await navigator.clipboard.writeText(id);
		copiedId = id;
		setTimeout(() => {
			if (copiedId === id) copiedId = '';
		}, 1500);
	}

	// Typing an exact id (e.g. one with a :free / :batch suffix that may not
	// be its own row) copies it without needing to find it in the list below.
	let manualId = $state('');

	const filtered = $derived.by(() => {
		if (!models) return [];
		const q = query.trim().toLowerCase();
		let rows = q
			? models.filter((m) => m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
			: models;
		if (showFavoritesOnly) rows = rows.filter((m) => favorites.has(m.id));

		return [...rows].sort((a, b) => {
			// starred models always float to the top, regardless of sort key —
			// that's the whole point of starring one
			const fa = favorites.has(a.id), fb = favorites.has(b.id);
			if (fa !== fb) return fa ? -1 : 1;

			if (sortKey === 'context') return b.context_length - a.context_length;
			if (sortKey === 'price') return Number(a.pricing.prompt) - Number(b.pricing.prompt);
			return a.name.localeCompare(b.name);
		});
	});
</script>

<div class="mx-auto flex max-w-4xl flex-col gap-4 p-6">
	<div>
		<h1 class="text-lg font-medium text-foreground">OpenRouter models</h1>
		<p class="text-sm text-muted-foreground">
			{models ? `${models.length} models` : 'Loading…'} — click a row to copy its id, then use it
			with ask_openrouter's <code class="text-xs">model</code> argument.
		</p>
	</div>

	{#if loadError}
		<Badge variant="destructive">{loadError}</Badge>
	{/if}
	{#if pinError}
		<Badge variant="destructive">{pinError}</Badge>
	{/if}

	<div class="flex items-center gap-2">
		<Input placeholder="Search by name or id…" class="max-w-sm" bind:value={query} />
		<div class="flex gap-1 text-xs">
			{#each [['name', 'Name'], ['context', 'Context'], ['price', 'Price']] as [key, label] (key)}
				<button
					type="button"
					class="rounded-md px-2 py-1 {sortKey === key
						? 'bg-foreground/10 text-foreground'
						: 'text-muted-foreground hover:bg-foreground/5'}"
					onclick={() => (sortKey = key as typeof sortKey)}
				>
					{label}
				</button>
			{/each}
			<button
				type="button"
				class="rounded-md px-2 py-1 {showFavoritesOnly
					? 'bg-foreground/10 text-foreground'
					: 'text-muted-foreground hover:bg-foreground/5'}"
				onclick={() => (showFavoritesOnly = !showFavoritesOnly)}
			>
				★ Favorites
			</button>
		</div>
	</div>

	<!-- Skip the list entirely — copy any id, e.g. one with a :free suffix,
	     without needing it to be its own row. -->
	<div class="flex items-center gap-2">
		<Input
			placeholder="Or type an exact model id, e.g. nvidia/nemotron-3-ultra-550b-a55b:free"
			class="text-sm"
			bind:value={manualId}
			onkeydown={(e) => e.key === 'Enter' && copyId(manualId.trim())}
		/>
		<Button
			size="sm"
			variant="secondary"
			disabled={!manualId.trim()}
			onclick={() => copyId(manualId.trim())}
		>
			{copiedId && copiedId === manualId.trim() ? 'Copied!' : 'Copy'}
		</Button>
	</div>

	{#if models}
		<Table.Root>
			<Table.Header>
				<Table.Row>
					<Table.Head class="w-8"></Table.Head>
					<Table.Head>Model</Table.Head>
					<Table.Head class="text-right">Context</Table.Head>
					<Table.Head class="text-right">$/1M in</Table.Head>
					<Table.Head class="text-right">$/1M out</Table.Head>
					<Table.Head></Table.Head>
				</Table.Row>
			</Table.Header>
			<Table.Body>
				{#each filtered.slice(0, 200) as model (model.id)}
					<Table.Row
						class="cursor-pointer"
						onclick={() => copyId(model.id)}
						title="Click to copy: {model.id}"
					>
						<Table.Cell class="w-8">
							<button
								type="button"
								class="text-base leading-none {favorites.has(model.id)
									? 'text-amber-500'
									: 'text-muted-foreground/40 hover:text-muted-foreground'}"
								title={favorites.has(model.id) ? 'Remove from favorites' : 'Add to favorites'}
								onclick={(e) => toggleFavorite(model.id, e)}
							>
								{favorites.has(model.id) ? '★' : '☆'}
							</button>
						</Table.Cell>
						<Table.Cell>
							<div class="flex flex-col">
								<span class="text-sm text-foreground">{model.name}</span>
								<span class="text-xs text-muted-foreground"
									>{copiedId === model.id ? 'Copied!' : model.id}</span
								>
							</div>
						</Table.Cell>
						<Table.Cell class="text-right text-sm tabular-nums text-muted-foreground">
							{model.context_length >= 1000
								? `${Math.round(model.context_length / 1000)}k`
								: model.context_length}
						</Table.Cell>
						<Table.Cell class="text-right text-sm tabular-nums text-muted-foreground"
							>{perMillion(model.pricing.prompt)}</Table.Cell
						>
						<Table.Cell class="text-right text-sm tabular-nums text-muted-foreground"
							>{perMillion(model.pricing.completion)}</Table.Cell
						>
						<Table.Cell class="text-right">
							<Button
								size="sm"
								variant={pinnedIds.has(model.id) ? 'default' : 'secondary'}
								class="h-7 px-2 text-[11px]"
								disabled={pinBusy[model.id]}
								title={pinnedIds.has(model.id)
									? 'Remove from normal chat model list'
									: 'Add to normal chat model list'}
								onclick={(e) => togglePin(model, e)}
							>
								{pinBusy[model.id] ? '…' : pinnedIds.has(model.id) ? 'Pinned ✓' : 'Pin to chat'}
							</Button>
						</Table.Cell>
					</Table.Row>
				{/each}
			</Table.Body>
		</Table.Root>

		{#if filtered.length > 200}
			<p class="text-center text-xs text-muted-foreground">
				Showing 200 of {filtered.length} matches — narrow the search to see more.
			</p>
		{:else if filtered.length === 0}
			<p class="text-center text-xs text-muted-foreground">No models match "{query}".</p>
		{/if}
	{/if}
</div>
