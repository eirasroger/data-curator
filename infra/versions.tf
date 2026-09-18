terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Local state, gitignored. A remote backend buys collaboration and locking,
  # and this project has one operator; a GCS bucket would be a resource to
  # create before the resources, and one more thing teardown has to remember.
}

provider "google" {
  project = var.project
  region  = var.region
}
