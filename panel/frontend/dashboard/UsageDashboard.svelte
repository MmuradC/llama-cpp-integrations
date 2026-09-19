<script lang="ts">
	// Usage dashboard for the New Chat screen: a GitHub-style calendar of the
	// last 90 days, coloured by requests per day (the one unit all three
	// providers share), with the current allowance beside it.
	//
	// Data comes from the panel backend: /api/usage-history (daily rollup,
	// recorded per request and backfilled where timestamps existed) and
	// /api/usage-summary (the live windows this machine already tracks).
	import { toast } from '$lib/utils/panel-command-runtime';

	const PANEL_ORIGIN = 'http://127.0.0.1:9010';
	const DAYS = 90;
	const WEEKDAY_LABELS = ['Mon', '', 'Wed', '', 'Fri', '', 'Sun'];
	const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

	interface DayBucket {
		cost: number;
		requests: number;
		tokens: number;
	}
	interface Day {
		date: string;
		providers: Record<string, DayBucket>;
		total: number;
	}
	interface Summary {
		openrouter_account?: { total_credits: number; total_usage: number } | null;
		providers: Record<string, Record<string, number | string | boolean> | undefined>;
	}

	let days = $state<Day[]>([]);
	let summary = $state<Summary | null>(null);
	let error = $state('');
	let loaded = $state(false);

	const maxRequests = $derived(Math.max(1, ...days.map((d) => d.total)));
	const activeDays = $derived(days.filter((d) => d.total > 0).length);

	/** Intensity 0-4, GitHub-style: empty, then quarters of the busiest day. */
	function level(count: number): number {
		if (count <= 0) return 0;

		const ratio = count / maxRequests;

		if (ratio > 0.75) return 4;
		if (ratio > 0.5) return 3;
		if (ratio > 0.25) return 2;

		return 1;
	}

	const LEVEL_CLASS = [
		'bg-muted/70 dark:bg-muted/40',
		'bg-emerald-500/20',
		'bg-emerald-500/40',
		'bg-emerald-500/65',
		'bg-emerald-500'
	];

	/** Columns of seven days, weeks left to right, Monday first like GitHub. */
	const weeks = $derived.by(() => {
		const columns: { day: Day | null; label: string }[][] = [];

		if (days.length === 0) return columns;

		// Monday = 0 ... Sunday = 6
		const firstWeekday = (new Date(`${days[0].date}T12:00:00`).getDay() + 6) % 7;
		let column: { day: Day | null; label: string }[] = Array.from(
			{ length: firstWeekday },
			() => ({ day: null, label: '' })
		);

		for (const day of days) {
			// the month label rides on the first column whose week contains the 1st
			column.push({ day, label: '' });

			if (column.length === 7) {
				columns.push(column);
				column = [];
			}
		}

		if (column.length > 0) {
			while (column.length < 7) column.push({ day: null, label: '' });
			columns.push(column);
		}

		// month label above the first week of each month
		let previousMonth = '';
		for (const week of columns) {
			const first = week.find((cell) => cell.day)?.day;

			if (!first) continue;

			const month = MONTHS[new Date(`${first.date}T12:00:00`).getMonth()];

			if (month !== previousMonth) {
				week[0] = { ...week[0], label: month };
				previousMonth = month;
			}
		}

		return columns;
	});

	const stats = $derived.by(() => {
		const sum = (n: number) => days.slice(-n).reduce((total, d) => total + d.total, 0);

		return [
			{ label: 'today', value: days.at(-1)?.total ?? 0 },
			{ label: '7 days', value: sum(7) },
			{ label: '30 days', value: sum(30) },
			{ label: `${DAYS} days`, value: sum(DAYS) }
		];
	});

	/** Per-provider totals for the window, busiest first, with a share bar. */
	const perProvider = $derived.by(() => {
		const sums = new Map<string, number>();

		for (const day of days) {
			for (const [name, bucket] of Object.entries(day.providers)) {
				sums.set(name, (sums.get(name) ?? 0) + bucket.requests);
			}
		}

		const total = [...sums.values()].reduce((a, b) => a + b, 0) || 1;

		return [...sums.entries()]
			.sort((a, b) => b[1] - a[1])
			.map(([name, count]) => ({ count, name, share: Math.round((count / total) * 100) }));
	});

	const PROVIDER_BAR: Record<string, string> = {
		nim: 'bg-emerald-500',
		opencode: 'bg-sky-500',
		openrouter: 'bg-amber-500'
	};

	const firstDay = $derived(days.find((d) => d.total > 0)?.date ?? null);

	function dayTitle(day: Day): string {
		const parts = Object.entries(day.providers).map(
			([name, bucket]) => `${name} ${bucket.requests}`
		);

		return `${day.date} — ${day.total} request${day.total === 1 ? '' : 's'}${parts.length ? ` (${parts.join(', ')})` : ''}`;
	}

	async function load(): Promise<void> {
		try {
			const [history, summaryResponse] = await Promise.all([
				fetch(`${PANEL_ORIGIN}/api/usage-history?days=${DAYS}`, {
					signal: AbortSignal.timeout(8000)
				}),
				fetch(`${PANEL_ORIGIN}/api/usage-summary`, { signal: AbortSignal.timeout(8000) })
			]);

			if (!history.ok) throw new Error(`HTTP ${history.status}`);

			days = ((await history.json()) as { days: Day[] }).days;
			summary = summaryResponse.ok ? ((await summaryResponse.json()) as Summary) : null;
			error = '';
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
		} finally {
			loaded = true;
		}
	}

	$effect(() => {
		void load();

		const timer = setInterval(() => void load(), 60000);

		return () => clearInterval(timer);
	});

	/** One allowance line per provider, from the live summary. */
	const allowances = $derived.by(() => {
		const rows: string[] = [];
		const providers = summary?.providers ?? {};

		for (const [name, value] of Object.entries(providers)) {
			if (!value) continue;

			if (typeof value.cost === 'number' && typeof value.requests === 'number') {
				rows.push(`${name} $${value.cost.toFixed(2)} · ${value.requests} req`);
			} else if (typeof value.count_5h === 'number') {
				rows.push(
					`${name} ${value.count_5h} in ${String(value.total_window ?? '5h')} · ${String(value.count_total ?? 0)} total`
				);
			}
		}

		const account = summary?.openrouter_account;

		if (account) {
			rows.push(
				`openrouter account $${account.total_usage.toFixed(2)} of $${account.total_credits.toFixed(0)}`
			);
		}

		return rows;
	});

	function openProviderPage(): void {
		const provider = perProvider[0]?.name;

		if (!provider) {
			toast.error('No usage recorded yet');

			return;
		}

		window.open(`${PANEL_ORIGIN}/${provider}-usage`, '_blank', 'noopener');
	}
</script>

<div
	class="border-border bg-background/95 supports-[backdrop-filter]:bg-background/85 pointer-events-auto rounded-2xl border shadow-lg backdrop-blur-xl"
>
	<header class="border-border/60 flex items-baseline justify-between gap-3 border-b px-4 py-2.5">
		<h2 class="text-sm font-semibold">Usage</h2>
		<p class="text-muted-foreground text-[11px]">
			requests per day · last {DAYS} days
		</p>
	</header>

	{#if error}
		<p class="text-muted-foreground px-4 py-3 text-xs">
			Panel backend unreachable ({error}) — the dashboard needs it running.
		</p>
	{:else if !loaded}
		<p class="text-muted-foreground px-4 py-3 text-xs">Loading…</p>
	{:else}
		<!-- The four figures in a single row, above everything else: they are the
		     summary, and the card is sized to whatever space is left under the
		     composer - on a short viewport that means it scrolls, and a summary
		     below the fold is no summary at all. -->
		<div class="grid grid-cols-4 gap-2 px-4 pt-3">
			{#each stats as stat (stat.label)}
				<div class="bg-muted/40 rounded-lg px-3 py-1.5">
					<div class="text-lg leading-tight font-semibold tabular-nums">{stat.value}</div>
					<div class="text-muted-foreground text-[10px]">{stat.label}</div>
				</div>
			{/each}
		</div>

		<div class="flex flex-col gap-3 px-4 py-3 lg:flex-row lg:items-start lg:gap-6">
			<!-- calendar -->
			<div class="min-w-0">
				<div class="flex gap-1.5">
					<!-- weekday gutter -->
					<div class="text-muted-foreground/70 flex flex-col gap-[3px] pt-4 text-[9px] leading-none">
						{#each WEEKDAY_LABELS as label, weekdayIndex (weekdayIndex)}
							<span class="h-3.5 leading-3.5">{label}</span>
						{/each}
					</div>

					<div class="scrollbar-thin overflow-x-auto pb-1">
						<div class="flex gap-[3px]">
							{#each weeks as week, weekIndex (weekIndex)}
								<div class="flex flex-col gap-[3px]">
									<span class="text-muted-foreground/70 h-3.5 text-[9px] leading-3.5">
										{week[0].label}
									</span>

									{#each week as cell, dayIndex (cell.day?.date ?? `pad-${weekIndex}-${dayIndex}`)}
										{#if cell.day}
											<div
												class="h-3.5 w-3.5 rounded-[3px] transition-transform hover:scale-125 hover:ring-1 hover:ring-foreground/40 {LEVEL_CLASS[
													level(cell.day.total)
												]}"
												title={dayTitle(cell.day)}
											></div>
										{:else}
											<div class="h-3.5 w-3.5"></div>
										{/if}
									{/each}
								</div>
							{/each}
						</div>
					</div>
				</div>

				<div class="text-muted-foreground mt-2 flex items-center gap-3 text-[10px]">
					<span class="flex items-center gap-1">
						Less
						{#each LEVEL_CLASS as cls (cls)}
							<span class="h-3 w-3 rounded-[3px] {cls}"></span>
						{/each}
						More
					</span>

					{#if firstDay}
						<span class="text-muted-foreground/70">
							{activeDays} active day{activeDays === 1 ? '' : 's'} since {firstDay}
						</span>
					{:else}
						<span class="text-muted-foreground/70">no usage recorded in this window yet</span>
					{/if}

					<span class="text-muted-foreground/70">busiest day {maxRequests}</span>
				</div>
			</div>

			<!-- per-provider split and allowances -->
			<div class="flex min-w-0 flex-1 flex-col gap-2">
				{#if perProvider.length > 0}
					<div class="flex flex-col gap-1.5">
						<div class="bg-muted/50 flex h-1.5 overflow-hidden rounded-full">
							{#each perProvider as provider (provider.name)}
								<div
									class="h-full {PROVIDER_BAR[provider.name] ?? 'bg-muted-foreground'}"
									style="width: {provider.share}%"
								></div>
							{/each}
						</div>

						<div class="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
							{#each perProvider as provider (provider.name)}
								<span class="flex items-center gap-1.5">
									<span
										class="h-2 w-2 rounded-full {PROVIDER_BAR[provider.name] ??
											'bg-muted-foreground'}"
									></span>
									{provider.name}
									<span class="text-foreground/80 tabular-nums">{provider.count}</span>
									<span class="text-muted-foreground/60">{provider.share}%</span>
								</span>
							{/each}
						</div>
					</div>
				{/if}

				{#if allowances.length > 0}
					<div class="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px]">
						{#each allowances as row, rowIndex (rowIndex)}
							<span class="tabular-nums">{row}</span>
						{/each}
					</div>
				{/if}

				<button
					type="button"
					class="text-muted-foreground hover:text-foreground self-start text-[10px] underline"
					onclick={openProviderPage}
				>
					Per-model detail
				</button>
			</div>
		</div>
	{/if}
</div>
