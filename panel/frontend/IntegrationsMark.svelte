<script lang="ts">
	// The dedicated brand mark for this integration set (llama.cpp/assets/),
	// imported by *relative* path — this file lives outside tools/ui, so `$lib`
	// resolves inside tools/ui and never reaches this PNG; a relative asset
	// import is location-independent the same way $app/navigation is.
	//
	// Transparent-backed variant, and that is the point of it.
	//
	// The source is a black PLATE with white strokes drawn on it (corners are
	// opaque rgb(17,17,17) — verified, not assumed). Rendered in the panel's
	// top row that plate shows as a dark square around the mark, which reads
	// as "the border is too small for the logo": the white shape does not
	// reach the plate's edge, because the artwork's diagonal composition
	// leaves ~17-18% padding on each side that no amount of -trim removes.
	//
	// Stripping the plate instead of resizing the mark is the fix that keeps
	// the shape's proportions: -fuzz 22% -fill none -opaque black turns the
	// background into real alpha (corners now alpha 0), so the strokes sit on
	// the panel's own background and there is no border to be too small.
	import integrationsLogo from '../../assets/llama-cpp-integrations-logo-clear.png';

	// Same prop contract as misc/Logo.svelte: class + style only, so it slots
	// into ActionIcon's `<IconComponent class={iconSize} />` render and into a
	// plain wordmark row.
	let { class: className = '', style = '' } = $props();
</script>

<img
	src={integrationsLogo}
	{style}
	alt="llama.cpp integrations"
	class="{className}"
	draggable="false"
/>

<style>
	/* NO width/height in this block, and that is the entire fix.
	 *
	 * Measured in Chromium against the running UI with Playwright, 2026-10-02:
	 *
	 *   left sidebar logo   <svg>                          16 x 16 px
	 *   this mark           <img class="h-12 w-12">        14 x 14 px
	 *
	 * The class said 48px and the element rendered at 14px. Cause: this scoped
	 * block declared width: var(--size, 1em). ActionIcon passes a CLASS and no
	 * style, so --size was unset, the 1em fallback applied, and a
	 * component-scoped rule beats a utility class on specificity. Every
	 * iconSize change at the call site was silently discarded.
	 *
	 * Sizing is therefore the caller's job in both shapes it can take:
	 *   - class-driven (ActionIcon): h-* / w-* utilities
	 *   - style-driven (wordmark):   inline --size on the element
	 *
	 * display:block stays: without it the inline-image baseline gap makes a
	 * box-sized image render a few px taller than the box it was given. */
	img {
		object-fit: contain;
		display: block;
	}
</style>
