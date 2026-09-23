terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Local state (gitignored); one operator needs no remote backend.
}

provider "google" {
  project = var.project
  region  = var.region
}
