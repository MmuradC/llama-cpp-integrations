<script lang="ts">
import { PANEL_ORIGIN } from './panel-origin';
	// Lives in panel/frontend, not the fork's UI tree — same reasoning as
	// OpenRouterPage.svelte: the only thing this integration adds inside
	// llama.cpp's own source is a two-line import in ChatFormActions.svelte
	// (see panel/patches/panel-ui.patch). Everything that actually renders
	// stays here, reachable through the @mcp-panel Vite alias.
	//
	// Sits immediately left of the context gauge in the chat form's action
	// row, and answers "how much of my remote allowance is gone" for the
	// model currently selected — the one thing the context gauge cannot say,
	// because a remote model's limits live at the provider, not in n_ctx.
	//
	// Renders nothing at all for a local model. A local llama.cpp model has
	// no remote allowance, so a badge there would be pure noise.
	import { onMount } from 'svelte';
	import { useContextGauge } from '$lib/hooks/use-context-gauge.svelte';

	type ProviderUsage =
		| {
				provider: 'opencode' | 'nim';
				kind: 'requests';
				real_id: string;
				count_5h: number;
				count_24h: number;
				count_total: number;
				limit_5h: number | null;
				limit_is_estimate: boolean;
				limit_known: boolean;
		  }
		| {
				provider: 'openrouter';
				kind: 'spend';
				real_id: string;
				cost: number;
				last_cost: number | null;
				requests: number;
				prompt_tokens: number;
				completion_tokens: number;
		  };

	const PANEL_URL = PANEL_ORIGIN;
	// One detail page per provider, served by the panel backend as plain
	// HTML. Clicking the badge opens the full table rather than growing a
	// popup inside the chat form — and those pages exist regardless of
	// whether this UI bundle has been rebuilt.
	const DETAIL_PAGES: Record<string, string> = {
		opencode: `${PANEL_URL}/opencode-usage`,
		nim: `${PANEL_URL}/nim-usage`,
		openrouter: `${PANEL_URL}/openrouter-usage`
	};

	// Matches the router's own registered-id prefixes (see server-models.cpp's
	// load_remote_model_presets_for) — a local model's id is just its name or
	// path, never one of these.
	const REMOTE_PREFIXES = ['openrouter/', 'nim/', 'opencode/'];

	const gauge = useContextGauge();

	let usage = $state<ProviderUsage | null>(null);
	let failed = $state(false);

	const modelId = $derived(gauge.activeModelId ?? '');
	const isRemote = $derived(REMOTE_PREFIXES.some((p) => modelId.startsWith(p)));

	async function refresh() {
		if (!isRemote || !modelId) {
			usage = null;

			return;
		}
		try {
			const res = await fetch(
				`${PANEL_URL}/api/provider-usage?model=${encodeURIComponent(modelId)}`,
				{ signal: AbortSignal.timeout(6000) }
			);
			if (!res.ok) throw new Error(String(res.status));
			usage = await res.json();
			failed = false;
		} catch {
			// Panel backend down is a normal state, not an error worth a
			// dialog — the badge just disappears (see the {#if} below).
			usage = null;
			failed = true;
		}
	}

	// Refetch on model switch. Deliberately not on every token: the counter
	// only changes when a request completes, which the interval below
	// catches.
	$effect(() => {
		void modelId;
		void refresh();
	});

	onMount(() => {
		const timer = setInterval(() => void refresh(), 15000);

		return () => clearInterval(timer);
	});

	// Fraction of the window's allowance consumed, 0..1 — only meaningful
	// for providers that publish a denominator (OpenCode's console estimate).
	// null means "no ring": either unlimited-known or genuinely unknown.
	const fraction = $derived.by(() => {
		if (!usage || usage.kind !== 'requests') return null;
		if (!usage.limit_5h || usage.limit_5h <= 0) return null;

		return Math.min(1, usage.count_5h / usage.limit_5h);
	});

	// Green under half, amber past it, red past four fifths — the same
	// three-step language the context gauge uses for its own dial, so the
	// two shapes sitting side by side read consistently.
	const ringColor = $derived(
		fraction === null
			? 'text-muted-foreground/40'
			: fraction >= 0.8
				? 'text-red-500'
				: fraction >= 0.5
					? 'text-amber-500'
					: 'text-emerald-500'
	);

	const label = $derived.by(() => {
		if (!usage) return '';
		if (usage.kind === 'spend') {
			// Sub-cent spend is the normal case on a cheap model, so
			// toFixed(2) would read as a flat "$0.00" and look broken.
			if (usage.cost === 0) return '$0';
			if (usage.cost < 0.01) return `$${usage.cost.toFixed(4)}`;

			return `$${usage.cost.toFixed(2)}`;
		}
		if (usage.limit_5h) return `${usage.count_5h}/${usage.limit_5h}`;

		return `${usage.count_5h} req`;
	});

	const tooltip = $derived.by(() => {
		if (!usage) return '';
		if (usage.kind === 'spend') {
			return [
				`OpenRouter · ${usage.real_id}`,
				`Spent on this model: $${usage.cost.toFixed(6)}`,
				usage.last_cost != null ? `Last message: $${usage.last_cost.toFixed(6)}` : '',
				`${usage.requests} requests · ${usage.prompt_tokens + usage.completion_tokens} tokens`,
				'Counted locally from each response\'s usage block; OpenRouter only reports account-wide totals.'
			]
				.filter(Boolean)
				.join('\n');
		}
		const lines = [
			`${usage.provider === 'nim' ? 'NVIDIA NIM' : 'OpenCode Go'} · ${usage.real_id}`,
			`${usage.count_5h} requests in the last 5h · ${usage.count_24h} in 24h`
		];
		if (usage.limit_5h) {
			lines.push(
				`Estimated 5h cap: ${usage.limit_5h}`,
				'Cap is an estimate read off the provider console, not a live limit.'
			);
		} else {
			lines.push('NVIDIA publishes no usage figure through its API, so no cap is shown.');
		}

		return lines.join('\n');
	});

	// Circumference of the r=5.5 ring below, for the dasharray trick.
	const RING_CIRCUMFERENCE = 2 * Math.PI * 5.5;
</script>

{#if usage}
	<button
		type="button"
		class="flex h-5 cursor-pointer items-center gap-1 rounded-full px-1 transition-colors hover:bg-foreground/5"
		title={tooltip}
		aria-label="{usage.provider} usage: {label}"
		onclick={() => window.open(DETAIL_PAGES[usage.provider], '_blank', 'noopener')}
	>
		{#if fraction !== null}
			<svg viewBox="0 0 14 14" class="h-3.5 w-3.5 -rotate-90" aria-hidden="true">
				<circle cx="7" cy="7" r="5.5" fill="none" stroke="currentColor" stroke-width="1.5"
					class="text-foreground/15" />
				<circle
					cx="7"
					cy="7"
					r="5.5"
					fill="none"
					stroke="currentColor"
					stroke-width="1.5"
					stroke-linecap="round"
					class={ringColor}
					stroke-dasharray="{RING_CIRCUMFERENCE}"
					stroke-dashoffset="{RING_CIRCUMFERENCE * (1 - fraction)}"
				/>
			</svg>
		{:else}
			<!-- No denominator to draw, so a plain dot carries the state
			     instead of a ring that would imply a fraction it doesn't have. -->
			<span class="h-1.5 w-1.5 rounded-full bg-foreground/30" aria-hidden="true"></span>
		{/if}
		<span class="text-[10px] font-medium tabular-nums text-muted-foreground">{label}</span>
	</button>
{/if}
