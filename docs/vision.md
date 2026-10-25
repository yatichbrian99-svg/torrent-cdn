# TorrentCDN — the north star

**Source:** the owner's own plan, written 2026-10-16. Recorded here with **wording unchanged** so it stays the canonical statement of intent, and so `docs/future-roadmap.md` can be measured against it rather than against anyone's guesses.

**Companion:** `docs/vision-one-pager.md` holds the short, outside-facing version of the same intent, also verbatim. This page is the long plan; that one is the summary.

Only formatting differs from the original text: markdown headings added, the ASCII diagram placed in a code fence, the space-separated category labels in §6 and §10 rendered in bold, and the quoted hypothetical lines (`«"I have this GPU available for 8 hours."»` etc.) rendered as blockquotes. No sentence has been added, removed, reordered or reworded.

---

## The big idea

TorrentCDN isn't supposed to just be a CDN.

The bigger goal is to build a company that makes infrastructure available from everywhere.

There are millions of computers, GPUs, servers, storage drives and internet connections sitting unused or underused.

The hardware already exists.

The problem is that all of it is disconnected.

I want TorrentCDN to be the layer that connects those resources and makes them usable by other people and businesses.

Basically:

Someone has spare resources → they provide them → someone else needs them → they pay → the provider earns.

TorrentCDN takes care of everything in between.

---

## 1. Start with what already works

The first product is TorrentCDN's streaming system.

It's already built, so the immediate goal isn't to keep adding features forever.

The next step is getting it onto a proper cloud machine instead of having my PC act as the public server.

The Oracle VM is basically the starting point.

Once it's running:

- Put TorrentCDN online
- Get actual users
- Test livestreaming
- Test P2P offload
- Monitor everything
- Find the first real bottleneck

Then fix the bottleneck.

I don't want to buy expensive servers before I actually know what I need.

---

## 2. Prove P2P actually works

The important thing isn't just getting people to watch a stream.

I want to see how much traffic the network can move between peers instead of everything coming from the origin.

Things to measure:

- Number of viewers
- Bandwidth being used
- P2P bandwidth
- Origin bandwidth
- Percentage of traffic offloaded
- Startup time
- Buffering
- Uptime
- Node failures
- Cost per viewer

If the system can reliably handle real traffic, that's the first major proof.

---

## 3. Then move beyond streaming

Once streaming is stable, the same network can be used for files.

For example:

- Large downloads
- Software
- Datasets
- Videos
- Backups
- Static files

Instead of everything coming from one server, different parts of the network can help deliver it.

That's where the company starts becoming more than a CDN.

---

## 4. Then AI compute

This is one of the areas I really want to explore.

There are people with powerful GPUs that sit unused for hours every day.

At the same time, there are people who want to run AI models but can't afford their own GPU.

TorrentCDN could connect those two sides.

Someone with a GPU could say:

> "I have this GPU available for 8 hours."

Someone else could say:

> "I need this model to process these requests."

TorrentCDN connects them.

The GPU owner earns money.

The customer pays for the compute.

TorrentCDN takes a percentage.

---

## 5. Local AI models

Another interesting use case is people running models locally.

Someone might have:

- A good GPU
- A local LLM
- A workstation
- A home server

They could choose to make some of that capacity available.

A customer could pay to use it.

The owner earns passive income from hardware they already own.

Obviously, this needs serious security and isolation before putting real customers on it.

---

## 6. The bigger marketplace

Eventually I want TorrentCDN to have two sides.

**People providing resources.** They can provide:

- CPU
- GPU
- Storage
- Bandwidth
- Servers

They choose what they're willing to provide and when.

**People buying resources.** They can buy:

- Compute
- Storage
- Bandwidth
- AI inference
- Content delivery
- Edge processing

TorrentCDN handles the matching.

---

## 7. The important part: trust

This is probably one of the hardest parts of the whole company.

You can't just let random computers run anything.

Providers need protection.

Customers need protection too.

So TorrentCDN needs:

- Sandboxing
- Isolation
- Encryption
- Authentication
- Resource limits
- Node reputation
- Monitoring
- Abuse detection
- Automatic failure handling

If a node disappears, the workload should be able to move somewhere else.

If a node behaves badly, it should lose access to valuable workloads.

---

## 8. Make joining ridiculously easy

One thing I really want is for someone to be able to turn their machine into a node without being an expert.

Something like: Download TorrentCDN Node → Sign in → Choose resources → Start earning.

Then they get a dashboard showing:

- CPU usage
- GPU usage
- Bandwidth
- Storage
- Jobs completed
- Earnings
- Uptime

The easier it is to join, the faster the network can grow.

---

## 9. The network becomes the real product

Eventually the individual applications aren't the most important thing.

Streaming is one application.

Files are another.

AI compute is another.

The underlying network is the actual platform.

Something like:

```
                 TORRENTCDN
                     │
        ┌────────────┼────────────┐
        │            │            │
     Streaming     Files          AI
        │            │            │
        └────────────┼────────────┘
                     │
              TorrentCDN Network
                     │
       ┌─────────────┼─────────────┐
       │             │             │
      CPU           GPU         Storage
       │             │             │
       └─────────────┼─────────────┘
                     │
                Provider Nodes
```

That's the thing I ultimately want.

---

## 10. How TorrentCDN makes money

There are several possibilities.

**Transaction fees.** Every time someone uses resources on the network, TorrentCDN takes a percentage.

**Business plans.** Businesses pay monthly for guaranteed capacity and additional features.

**Enterprise.** Larger customers can get:

- Dedicated capacity
- Private networks
- SLAs
- Support
- Higher limits

**Premium infrastructure.** Charge for things like:

- Low latency
- Specific geographic locations
- Guaranteed GPU capacity
- High availability

---

## 11. Don't build everything at once

The temptation will be to build everything immediately.

Don't.

The order should probably be: Streaming → prove network → Files → prove marketplace → AI compute → expand infrastructure

Each stage should make the next one easier.

---

## 12. Grow infrastructure only when necessary

At the beginning: Oracle VM.

Then: first bottleneck.

Then buy whatever fixes that bottleneck.

Maybe it's:

- More CPU
- More RAM
- Better networking
- Faster storage
- A dedicated server
- More geographic locations

I don't want to spend money just to say we have servers.

I want the infrastructure to be paid for by actual usage.

---

## 13. The numbers matter

As the network grows, I want to know things like:

How many nodes do we have?

How much bandwidth can we provide?

How much storage is available?

How many GPUs are available?

How much traffic is being offloaded?

How much are providers earning?

How much revenue does TorrentCDN make?

How much does it cost to provide the service?

Those numbers will tell us whether the idea is actually working.

---

## 14. The long-term idea

The internet currently relies heavily on centralized infrastructure.

I don't think it has to stay that way.

There is already an enormous amount of unused computing power around the world.

The opportunity is to connect it.

Instead of asking:

> "Which server are we going to buy?"

The future could be:

> "How much infrastructure do we need?"

And TorrentCDN finds it.

---

## 15. The ultimate goal

I don't want TorrentCDN to just become another company renting servers.

I want it to become the network that lets other people turn their unused resources into infrastructure.

Someone has a GPU.

Someone has storage.

Someone has bandwidth.

Someone has a server.

Someone needs compute.

TorrentCDN connects them.

The hardware already exists.

We're building the layer that makes it useful.

---

## The rule

Every feature we build should make at least one of these better:

Cheaper. Faster. More reliable. More useful. Easier to join.

If it doesn't, it can wait.

**The first mission is simple:** get TorrentCDN off my PC, onto a stable VM, and into the real world.

Then let the network grow from there.
