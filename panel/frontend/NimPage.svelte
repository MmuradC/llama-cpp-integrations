<script lang="ts">
import { PANEL_ORIGIN } from './panel-origin';
import { syncMainFavorites } from './pinned-favorites';
	// Lives entirely outside llama.cpp's own source tree — see ../README.md.
	// Mirrors OpenRouterPage.svelte's structure exactly (same pin/favorite/
	// copy pattern against a second provider) — see that file for the
	// reasoning behind each piece; only commented here where NIM's simpler
	// catalog shape (just id/object, no pricing or context_length) changes
	// something. Reached from the "Browse NVIDIA NIM models" link in
	// RightBar.svelte. The route file that makes this discoverable to
	// SvelteKit's router is tools/ui/src/routes/nim/+page.svelte.
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';
	import * as Table from '$lib/components/ui/table';
	import ProviderUsagePanel from './ProviderUsagePanel.svelte';
	import { onMount } from 'svelte';

	const MODELS_URL = `${PANEL_ORIGIN}/api/nim/models`;
	const PINNED_URL = `${PANEL_ORIGIN}/api/nim/pinned`;
	const CONTEXT_LIMITS_URL = `${PANEL_ORIGIN}/api/context-limits`;
	const PIN_URL = `${PANEL_ORIGIN}/api/nim/pin`;
	const UNPIN_URL = `${PANEL_ORIGIN}/api/nim/unpin`;

	type NimModel = { id: string };

	let models = $state<NimModel[] | null>(null);
	let loadError = $state('');
	let query = $state('');
	let copiedId = $state('');
	let showFavoritesOnly = $state(false);

	const FAVORITES_KEY = 'nim-favorite-models';
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
			if (body.error && (!body.data || body.data.length === 0)) throw new Error(body.error);
			models = body.data ?? [];
		} catch (err) {
			loadError = err instanceof Error ? err.message : 'Failed to load';
		}
	}

	let pinnedIds = $state<Set<string>>(new Set());
	let pinBusy = $state<Record<string, boolean>>({});
	let pinError = $state('');

	/**
	 * Context windows we know for pinned NIM models, keyed by the registered
	 * id the router uses (`nim/<real id with any / as __>`). NVIDIA's own API
	 * publishes no context field, so a value here comes only from a real
	 * overflow being observed by the panel relay - where none is known the
	 * column shows an honest dash rather than a figure.
	 */
	let contextLimits: Record<string, { context: number; source: string }> = $state({});

	function registeredId(realId: string): string {
		return 'nim/' + realId.replace(/\//g, '__');
	}

	function contextLabel(realId: string): string {
		const hit = contextLimits[registeredId(realId)];
		if (!hit) return '-';
		return hit.context >= 1000 ? `${Math.round(hit.context / 1000)}k` : String(hit.context);
	}

	async function loadContextLimits() {
		try {
			const res = await fetch(CONTEXT_LIMITS_URL, { signal: AbortSignal.timeout(6000) });
			const body: { models: Record<string, { context: number; source: string }> } =
				await res.json();
			contextLimits = body.models ?? {};
		} catch {
			// the column is auxiliary; a dead backend just leaves dashes
		}
	}

	async function loadPinned() {
		try {
			const res = await fetch(PINNED_URL, { signal: AbortSignal.timeout(6000) });
			const body: { id: string }[] = await res.json();
			pinnedIds = new Set(body.map((e) => e.id));
		} catch {
			// pin buttons just show as unpinned until the backend is reachable
		}
	}

	async function togglePin(model: NimModel, e?: Event) {
		e?.stopPropagation();
		const wasPinned = pinnedIds.has(model.id);
		pinBusy = { ...pinBusy, [model.id]: true };
		pinError = '';
		try {
			const res = await fetch(wasPinned ? UNPIN_URL : PIN_URL, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ id: model.id, name: model.id }),
				signal: AbortSignal.timeout(10000)
			});
			if (!res.ok) throw new Error((await res.json().catch(() => null))?.error ?? res.statusText);

			const next = new Set(pinnedIds);
			if (wasPinned) next.delete(model.id);
			else next.add(model.id);
			pinnedIds = next;
			// A pin is a favorite — fill the main dropdown's heart (see pinned-favorites).
			syncMainFavorites(registeredId(model.id), !wasPinned);
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
		void loadContextLimits();
	});

	async function copyId(id: string) {
		await navigator.clipboard.writeText(id);
		copiedId = id;
		setTimeout(() => {
			if (copiedId === id) copiedId = '';
		}, 1500);
	}

	let manualId = $state('');

	const filtered = $derived.by(() => {
		if (!models) return [];
		const q = query.trim().toLowerCase();
		let rows = q ? models.filter((m) => m.id.toLowerCase().includes(q)) : models;
		if (showFavoritesOnly) rows = rows.filter((m) => favorites.has(m.id));

		return [...rows].sort((a, b) => {
			const fa = favorites.has(a.id), fb = favorites.has(b.id);
			if (fa !== fb) return fa ? -1 : 1;
			return a.id.localeCompare(b.id);
		});
	});
</script>

<div class="mx-auto flex max-w-4xl flex-col gap-4 p-6">
	<!-- Usage for this provider, above the catalog — shared with the
	     OpenCode page via ProviderUsagePanel, which also documents why
	     each provider gets a different shape (this one has no published usage figure at all). -->
	<ProviderUsagePanel title="NVIDIA NIM requests"
		url={`${PANEL_ORIGIN}/api/usage-summary`}
		provider="nim" />

	<div>
		<h1 class="text-lg font-medium text-foreground">NVIDIA NIM models</h1>
		<p class="text-sm text-muted-foreground">
			{models ? `${models.length} models` : 'Loading…'} — click a row to copy its id, then use it
			with ask_nim's <code class="text-xs">model</code> argument, or pin it into the normal chat
			model list.
		</p>
	</div>

	{#if loadError}
		<Badge variant="destructive">{loadError}</Badge>
	{/if}
	{#if pinError}
		<Badge variant="destructive">{pinError}</Badge>
	{/if}

	<div class="flex items-center gap-2">
		<Input placeholder="Search by id…" class="max-w-sm" bind:value={query} />
		<button
			type="button"
			class="rounded-md px-2 py-1 text-xs {showFavoritesOnly
				? 'bg-foreground/10 text-foreground'
				: 'text-muted-foreground hover:bg-foreground/5'}"
			onclick={() => (showFavoritesOnly = !showFavoritesOnly)}
		>
			★ Favorites
		</button>
	</div>

	<!-- Skip the list entirely — copy any id you already know without
	     needing it to be its own row. -->
	<div class="flex items-center gap-2">
		<Input
			placeholder="Or type an exact model id, e.g. nvidia/llama-3.3-nemotron-super-49b-v1.5"
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
		<!-- long real ids: let the table scroll on a phone instead of squishing -->
		<div class="w-full overflow-x-auto">
			<Table.Root class="min-w-[40rem]">
			<Table.Header>
				<Table.Row>
					<Table.Head class="w-8"></Table.Head>
					<Table.Head>Model</Table.Head>
					<Table.Head class="text-right">Context</Table.Head>
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
						<Table.Cell class="text-right text-sm tabular-nums text-muted-foreground">
							{contextLabel(model.id)}
						</Table.Cell>
						<Table.Cell>
							<span class="text-sm text-foreground"
								>{copiedId === model.id ? 'Copied!' : model.id}</span
							>
						</Table.Cell>
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
		</div>

		{#if filtered.length > 200}
			<p class="text-center text-xs text-muted-foreground">
				Showing 200 of {filtered.length} matches — narrow the search to see more.
			</p>
		{:else if filtered.length === 0}
			<p class="text-center text-xs text-muted-foreground">No models match "{query}".</p>
		{/if}
	{/if}
</div>
