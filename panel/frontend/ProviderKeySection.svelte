<script lang="ts">
	// The API key editor for one remote provider, as it lives on that
	// provider's own storefront page (OpenCode/OpenRouter/NIM) rather than in
	// the right bar. Moved here from right bar to keep the sidebar a list of
	// links; the key still POSTs straight to disk through the panel backend
	// and is never echoed back — this component shows a status badge plus a
	// submit-once input that clears on save.
	//
	// provider id matches the backend's SECRET_FILES keys
	// ('opencode' | 'openrouter' | 'nim') and the file names stored under
	// ~/.config/mcp-secrets.
	import { Badge } from '$lib/components/ui/badge';
	import { Button } from '$lib/components/ui/button';
	import { Input } from '$lib/components/ui/input';

	const PANEL_ORIGIN = 'http://127.0.0.1:9010';
	const SECRETS_URL = `${PANEL_ORIGIN}/api/secrets`;

	let { provider }: { provider: string } = $props();

	const LABELS: Record<string, string> = {
		opencode: 'OpenCode',
		openrouter: 'OpenRouter',
		nim: 'NVIDIA NIM'
	};

	let set = $state(false);
	let draft = $state('');
	let busy = $state(false);
	let message = $state('');

	async function refreshStatus() {
		try {
			const res = await fetch(SECRETS_URL, { signal: AbortSignal.timeout(6000) });
			const status: Record<string, boolean> = await res.json();
			set = Boolean(status[provider]);
		} catch {
			// backend unreachable: show the unset badge and let Save surface
			// the real error if the user still tries
		}
	}

	async function save() {
		const value = draft.trim();
		if (!value) return;

		busy = true;
		message = '';
		try {
			const res = await fetch(`${SECRETS_URL}/${provider}`, {
				method: 'POST',
				headers: { 'Content-Type': 'application/json' },
				body: JSON.stringify({ value }),
				signal: AbortSignal.timeout(6000)
			});
			if (!res.ok) throw new Error((await res.json().catch(() => null))?.error ?? res.statusText);

			draft = '';
			message = 'Saved';
			await refreshStatus();
		} catch (err) {
			message = err instanceof Error ? err.message : 'Failed';
		} finally {
			busy = false;
		}
	}

	refreshStatus();
</script>

<div>
	<h2 class="text-sm font-medium text-foreground">{LABELS[provider] ?? provider} API key</h2>
	<p class="text-sm text-muted-foreground">
		Stored on this machine via the panel backend — the value is never read back to the browser.
	</p>
	<div class="mt-2 flex items-center gap-2">
		{#if set}
			<Badge variant="secondary" class="shrink-0">configured</Badge>
		{:else}
			<Badge variant="outline" class="shrink-0">not set</Badge>
		{/if}
		<Input
			type="password"
			autocomplete="new-password"
			placeholder="sk-..."
			class="max-w-sm"
			bind:value={draft}
			onkeydown={(e) => e.key === 'Enter' && save()}
		/>
		<Button size="sm" variant="secondary" disabled={!draft.trim() || busy} onclick={() => save()}>
			{busy ? '…' : 'Save'}
		</Button>
	</div>
	{#if message}
		<p class="mt-1 text-xs {message === 'Saved' ? 'text-emerald-500' : 'text-destructive'}">
			{message}
		</p>
	{/if}
</div>
