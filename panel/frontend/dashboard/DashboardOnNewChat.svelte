<script lang="ts">
	// Shows the usage dashboard on the New Chat screen and nowhere else.
	//
	// Rendered by RightBar.svelte outside its expanded/collapsed branch, so it
	// is mounted whatever the panel is doing. It cannot be a sibling of the
	// composer (that would mean editing llama.cpp's screen), so it overlays the
	// free space *below* the composer - and the size of that space is measured
	// rather than assumed: a fixed height cap still reached up into the text
	// area on shorter viewports, because the composer sits lower there. The
	// card is given exactly the room that is actually left, and is not drawn at
	// all when there is too little of it.
	import { page } from '$app/state';
	import { conversationsStore } from '$lib/stores';
	import UsageDashboard from './UsageDashboard.svelte';

	/** Gap kept between the composer and the card, in pixels. */
	const GAP_PX = 16;
	/** Below this there is no room for a useful card, so it stays hidden. */
	const MIN_HEIGHT_PX = 140;

	let available = $state(0);

	/**
	 * The start screen: no conversation is open, and the route is not a chat.
	 * Both are needed - opening New Chat clears the active conversation before
	 * any chat URL is involved, and a chat that is still loading has one.
	 */
	const onStartScreen = $derived(
		conversationsStore.activeConversation === null && !page.url.hash.includes('/chat/')
	);

	$effect(() => {
		if (!onStartScreen) return;

		function measure(): void {
			const composer = document.querySelector('form');

			if (!composer) {
				available = 0;

				return;
			}

			const space = window.innerHeight - composer.getBoundingClientRect().bottom - GAP_PX;

			available = space >= MIN_HEIGHT_PX ? space : 0;
		}

		measure();

		// The composer moves as hints appear (a working-directory chip, the
		// scroll hint) and the window can be resized, so keep measuring.
		const timer = setInterval(measure, 500);

		window.addEventListener('resize', measure);

		return () => {
			clearInterval(timer);
			window.removeEventListener('resize', measure);
		};
	});
</script>

{#if onStartScreen && (inline || available > 0)}
	{#if inline}
		<div class="px-1 pb-2">
			<UsageDashboard />
		</div>
	{:else}
		<div
			class="pointer-events-none fixed left-1/2 z-10 w-[min(64rem,calc(100vw-4rem))] -translate-x-1/2 overflow-y-auto"
			style="bottom: {GAP_PX}px; max-height: {available}px;"
		>
			<UsageDashboard />
		</div>
	{/if}
{/if}
