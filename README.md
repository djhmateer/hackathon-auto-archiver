<h1>Hackathon</h1>

I work in human rights digital archiving and monitoring, with charitable investigative organisations in human rights and law, mostly in the UK. For the past 5 years, starting with https://osr4rightstools.org/, I've been using Bellingcat's open source auto-archiver through my fork: https://github.com/djhmateer/auto-archiver

In 2024 we won the ESRC Celebrating Impact Prize for "Research that is helping the UN and courts around the world to assess mobile phone evidence in war crimes prosecutions and other human rights cases"
https://www.swansea.ac.uk/press-office/news-events/news/2024/11/research-on-using-mobile-phone-evidence-in-human-rights-cases-wins-prestigious-impact-prize.php

Over the last year I've archived over 60,000 urls on 5 VM's running on a Proxmox hypervisor on my own hardware. Investigators are frequently waiting for archiving to finish whereas the other VM's are idle.

In this hackathon I'm building

1) a queuing system - a Postgres database and an overall coordinator service (on a separate VM). Then 5 worker VM's each have a new wrapper which takes work from Postgres and runs the existing archiver tool.

2) distressing image analyser. A problem for human rights investigators is that the imagery/videos can be frequently distressing. Commonly there is a 1-5 rating scale against all images (currently done manually) so others don't have to see the image unless they have to. This is also a problem for me as a developer, where I don't want to mistakenly see an image unless necessary. I wish to be forewarned. I'm experimenting with muse-glimmer:30b-q4_K_M running locally on Ollama with good (albeit slow on my CPU only) results

If anyone else is working or interested (or if I can help you) in digital archiving / monitoring / human rights , or if you'd like to chat, please drop me a line. dave@hmsoftware.co.uk or here on Discord.

Good luck everyone and I hope you're enjoying building.

Big thanks to Meta for this hackathon which has inspired me to start these 2 features.

<h1>This Fork</h1>

This repo is a copy of my fork, taken on 31st Oct 26 for the hackathon. It's version 1.2.9 plus the work above, which isn't in production yet.




<h1 align="center">Auto Archiver</h1>

[![Documentation Status](https://readthedocs.org/projects/auto-archiver/badge/?version=latest)](https://auto-archiver.readthedocs.io/en/latest/?badge=latest)
[![PyPI version](https://badge.fury.io/py/auto-archiver.svg)](https://badge.fury.io/py/auto-archiver)
[![Docker Image Version (latest by date)](https://img.shields.io/docker/v/bellingcat/auto-archiver?sort=semver&logo=docker&color=#69F0AE)](https://hub.docker.com/r/bellingcat/auto-archiver)
[![Core Test Status](https://github.com/bellingcat/auto-archiver/workflows/Core%20Tests/badge.svg)](https://github.com/bellingcat/auto-archiver/actions/workflows/tests-core.yaml)
<!-- [![Download Test Status](https://github.com/bellingcat/auto-archiver/workflows/Download%20Tests/badge.svg)](https://github.com/bellingcat/auto-archiver/actions/workflows/tests-download.yaml) -->

<!-- ![Docker Pulls](https://img.shields.io/docker/pulls/bellingcat/auto-archiver) -->
<!-- [![PyPI download month](https://img.shields.io/pypi/dm/auto-archiver.svg)](https://pypi.python.org/pypi/auto-archiver/) -->


Auto Archiver is a Python tool to automatically archive content on the web in a secure and verifiable way. It takes URLs from different sources (e.g. a CSV file, Google Sheets, command line etc.) and archives the content of each one. It can archive social media posts, videos, images and webpages. Content can be enriched, then saved either locally or remotely (S3 bucket, Google Drive). The status of the archiving process can be appended to a CSV report, or if using Google Sheets – back to the original sheet.

<div class="hidden_rtd">
  
**[See the Auto Archiver documentation for more information.](https://auto-archiver.readthedocs.io/en/latest/)**

</div>

Read the [article about Auto Archiver on bellingcat.com](https://www.bellingcat.com/resources/2022/09/22/preserve-vital-online-content-with-bellingcats-auto-archiver-tool/).


## Installation

View the [Installation Guide](https://auto-archiver.readthedocs.io/en/latest/installation/installation.html) for full instructions

**Advanced:**

To get started quickly using Docker:

`docker pull bellingcat/auto-archiver && docker run -it --rm -v secrets:/app/secrets bellingcat/auto-archiver --config secrets/orchestration.yaml`

Or pip:

`pip install auto-archiver && auto-archiver --help`

## Contributing

We welcome contributions to the Auto Archiver project! See the [Contributing Guide](https://auto-archiver.readthedocs.io/en/latest/contributing.html) for how to get involved!

