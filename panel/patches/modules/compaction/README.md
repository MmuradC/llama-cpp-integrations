# compaction

**Applies to:** NOT YET EXTRACTED — the patch file for this module still has to be generated against the tree a given build uses (see ../README.md). What follows describes the change it must contain.

```sh
git apply panel/patches/modules/compaction/compaction.patch   # once generated
```

## What it does

Summarize a long conversation, then resend only the summary onward.

## The change, precisely

chatStore.compactConversation() asks the model for a summary via ChatService.generateSummary, stores it as a synthetic message, and points the conversation's compactedThroughMessageId at it; streamChatCompletion trims the outgoing request to that message. Nothing is deleted - the originals stay in IndexedDB, just not resent. It also carries the zero-context fix: a remote model reports n_ctx 0, and treating that as a real budget made every single message its own API call (a 229-message conversation became 229 calls).
