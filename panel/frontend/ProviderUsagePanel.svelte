<script lang="ts">
	// Usage dashboard shown at the top of each provider page (OpenCode,
	// OpenRouter, NIM).
	//
	// One component rather than three copies: the three pages are otherwise
	// near-identical (same table, same pin flow) and their usage blocks would
	// have diverged the first time one of them gained a field. What differs
	// between providers is *which* figures exist, and that is expressed here
	// through `kind` rather than through three sets of markup:
	//
	//   kind: "windows"  allowance windows with a percent and a reset instant
	//                    (OpenCode Go — the only provider publishing one)
	//   kind: "spend"    USD amounts, optionally against a real credit total
	//                    (OpenRouter — API gives account-wide credits only)
	//   kind: "counts"   local request counts over two windows, no denominator
	//                    (NIM — its API exposes no usage figure at all)
	//
	// Each of those is an honest description of what its provider actually
	// reports; none of them is presented as a proportion when it is not one.
	import { Badge } from '$lib/components/ui/badge';
	import { onMount } from 'svelte';

	export type UsageWindow = {
		window: string;
		status: string;
		percent: number;
		resets_at: string | null;
	};

	export type UsageData = {
		kind: 'windows' | 'spend' | 'counts';
		/** windows */ windows?: UsageWindow[];
		/** spend */ cost?: number;
		requests?: number;
		tokens?: number;
		creditTotal?: number;
		creditUsed?: number;
		/** counts */ count5h?: number;
		count24h?: number;
		countTotal?: number;
		limitKnown?: boolean;
		/** every kind */ fetchedAt?: number;
		note?: string;
	};

	interface Props {
		title: string;
		/** Endpoint returning UsageData, or the shapes the backend already
		 *  returns for a given provider — see the loader functions below. */
		url: string;
		/** Which provider's fields to pluck out of the response, for the
		 *  endpoints that answer with all three at once (/api/usage-summary). */
		provider?: 'opencode' | 'openrouter' | 'nim';
		/** Shown when the fetch fails; some providers have no endpoint yet. */
		unavailableNote?: string;
	}

	let { title, url, provider, unavailableNote = '' }: Props = $props();

	let data = $state<UsageData | null>(null);
	let error = $state('');
	let loading = $state(true);

	const WINDOW_LABELS: Record<string, string> = {
		rolling: 'Rolling 5h',
		weekly: 'Weekly',
		monthly: 'Monthly'
	};

	/** Duration until a reset, not a timestamp: the question an allowance
	 *  raises is "how long until this frees up", and a duration is both
	 *  shorter and free of locale/clock disagreement between the panel and
	 *  whichever browser is looking at it. */
	function untilReset(iso: string | null): string {
		if (!iso) return '';
		const ms = new Date(iso).getTime() - Date.now();
		if (!Number.isFinite(ms) || ms <= 0) return 'resetting…';
		const m = Math.round(ms / 60000);
		if (m < 60) return `in ${m}m`;
		const h = Math.floor(m / 60);
		if (h < 48) return `in ${h}h ${m % 60}m`;
		return `in ${Math.round(h / 24)}d`;
	}

	/** Green under 60%, amber to 85%, red above — the same three-step
	 *  vocabulary the panel's badges use for fine / watch / act. */
	function barClass(percent: number): string {
		if (percent >= 85) return 'bg-destructive';
		if (percent >= 60) return 'bg-amber-500';
		return 'bg-emerald-500';
	}

	function money(v: number): string {
		if (v === 0) return '$0';
		return v < 0.01 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
	}

	async function load() {
		loading = true;
		try {
			const res = await fetch(url, { signal: AbortSignal.timeout(15000) });
			const body = await res.json();
			if (!res.ok && !body?.providers) throw new Error(body?.error ?? `HTTP ${res.status}`);
			data = normalise(body);
			error = '';
		} catch (err) {
			error = err instanceof Error ? err.message : 'Failed to load';
		} finally {
			loading = false;
		}
	}

	/**
	 * Map each endpoint's own response shape onto UsageData.
	 *
	 * Two families: the per-provider endpoints (/api/opencode/plan-usage,
	 * /api/nim/usage) answer in their own shape, while /api/usage-summary
	 * returns all three providers at once and is picked apart by `provider`.
	 * Keeping the translation here means the markup above never has to know
	 * which endpoint it was handed.
	 */
	function normalise(body: any): UsageData {
		// OpenCode Go plan: { keys: [{window, status, percent, resets_at}] }
		if (Array.isArray(body?.keys)) {
			return {
				kind: 'windows',
				windows: body.keys,
				fetchedAt: (body.fetched_at ?? 0) * 1000,
				note:
					"Live from OpenCode's own /usage — not a local estimate. " +
					'The monthly window is the binding one; 5h and weekly reset far sooner.'
			};
		}

		// /api/usage-summary: pick this provider out of the combined answer.
		const p = provider ? body?.providers?.[provider] : body;
		if (!p) return { kind: 'counts', note: 'No usage fields in response.' };

		if (p.kind === 'spend') {
			const acct = body?.openrouter_account;
			return {
				kind: 'spend',
				cost: p.cost ?? 0,
				requests: p.requests ?? 0,
				tokens: p.tokens ?? 0,
				creditTotal: acct?.total_credits,
				creditUsed: acct?.total_usage,
				fetchedAt: (acct?.fetched_at ?? 0) * 1000,
				note:
					acct?.total_credits > 0
						? 'Credit figures are account-wide and come from OpenRouter itself; the per-model spend is summed locally from each response\'s usage block.'
						: 'Summed locally from each response\'s usage block — OpenRouter publishes no per-key spend.'
			};
		}

		return {
			kind: 'counts',
			count5h: p.count_5h ?? 0,
			count24h: p.count_24h ?? 0,
			countTotal: p.count_total ?? 0,
			limitKnown: p.limit_known ?? false,
			note: p.limit_known
				? 'Accepted requests this box sent, counted locally. Each model caps separately.'
				: 'NVIDIA publishes no usage or credit figure through its API (checked live), so these are the requests this box sent — all that is obtainable programmatically.'
		};
	}

	onMount(() => {
		void load();
	});
</script>

<section class="rounded-lg border border-border bg-muted/30 p-3">
	<div class="mb-2 flex items-baseline justify-between gap-2">
		<h2 class="text-sm font-medium text-foreground">{title}</h2>
		<div class="flex items-center gap-2">
			{#if data?.fetchedAt}
				<span class="text-[11px] text-muted-foreground"
					>updated {new Date(data.fetchedAt).toLocaleTimeString()}</span
				>
			{/if}
			<button
				type="button"
				class="text-[11px] text-muted-foreground underline hover:text-foreground"
				onclick={() => load()}>refresh</button
			>
		</div>
	</div>

	{#if error}
		<div class="flex items-center gap-2">
			<Badge variant="destructive" class="text-[10px]">{error}</Badge>
			{#if unavailableNote}
				<span class="text-[11px] text-muted-foreground">{unavailableNote}</span>
			{/if}
		</div>
	{:else if loading && !data}
		<p class="text-xs text-muted-foreground">Loading…</p>
	{:else if data?.kind === 'windows'}
		<div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
			{#each data.windows ?? [] as w (w.window)}
				<div class="flex flex-col gap-1">
					<div class="flex items-baseline justify-between gap-2">
						<span class="text-[11px] text-muted-foreground"
							>{WINDOW_LABELS[w.window] ?? w.window}</span
						>
						<!-- Percent USED, matching the provider's own API and the
						     right bar's row. A remaining figure here would
						     disagree with the sidebar two clicks away. -->
						<span class="text-sm tabular-nums text-foreground">{w.percent.toFixed(0)}%</span>
					</div>
					<div class="h-1.5 w-full overflow-hidden rounded-full bg-foreground/10">
						<div
							class="h-full rounded-full {barClass(w.percent)}"
							style="width: {Math.min(100, Math.max(2, w.percent))}%"
						></div>
					</div>
					<div class="flex items-baseline justify-between gap-2">
						<span class="text-[10px] text-muted-foreground"
							>used · resets {untilReset(w.resets_at)}</span
						>
						{#if w.status && w.status !== 'ok'}
							<Badge variant="secondary" class="text-[10px]">{w.status}</Badge>
						{/if}
					</div>
				</div>
			{/each}
		</div>
	{:else if data?.kind === 'spend'}
		<div class="flex flex-wrap items-baseline gap-x-6 gap-y-2">
			<div class="flex flex-col">
				<span class="text-[11px] text-muted-foreground">Account credits</span>
				<span class="text-lg tabular-nums text-foreground">
					{#if data.creditTotal && data.creditTotal > 0}
						{money(data.creditUsed ?? 0)}<span class="text-sm text-muted-foreground"
							> / {money(data.creditTotal)}</span
						>
					{:else}
						{money(data.cost ?? 0)}
					{/if}
				</span>
			</div>
			{#if data.creditTotal && data.creditTotal > 0}
				{@const left = Math.max(0, data.creditTotal - (data.creditUsed ?? 0))}
				<div class="flex min-w-[12rem] flex-1 flex-col gap-1">
					<div class="h-1.5 w-full overflow-hidden rounded-full bg-foreground/10">
						<div
							class="h-full rounded-full {barClass(
								((data.creditUsed ?? 0) / data.creditTotal) * 100
							)}"
							style="width: {Math.min(
								100,
								Math.max(2, ((data.creditUsed ?? 0) / data.creditTotal) * 100)
							)}%"
						></div>
					</div>
					<span class="text-[10px] text-muted-foreground">{money(left)} remaining</span>
				</div>
			{/if}
			<div class="flex flex-col">
				<span class="text-[11px] text-muted-foreground">This box</span>
				<span class="text-sm tabular-nums text-foreground"
					>{data.requests ?? 0} req · {((data.tokens ?? 0) / 1000).toFixed(0)}k tok</span
				>
			</div>
		</div>
	{:else if data?.kind === 'counts'}
		<div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
			<div class="flex flex-col">
				<span class="text-[11px] text-muted-foreground">Requests · 5h</span>
				<span class="text-lg tabular-nums text-foreground">{data.count5h ?? 0}</span>
			</div>
			<div class="flex flex-col">
				<span class="text-[11px] text-muted-foreground">Requests · 24h</span>
				<span class="text-lg tabular-nums text-foreground">{data.count24h ?? 0}</span>
			</div>
			<div class="flex flex-col">
				<span class="text-[11px] text-muted-foreground">Total counted</span>
				<span class="text-lg tabular-nums text-foreground">{data.countTotal ?? 0}</span>
			</div>
		</div>
	{/if}

	{#if data?.note && !error}
		<p class="mt-2 text-[10px] text-muted-foreground">{data.note}</p>
	{/if}
</section>
