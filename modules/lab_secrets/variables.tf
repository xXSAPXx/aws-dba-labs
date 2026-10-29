variable "name_prefix" {
  description = "Start of every secret's name, e.g. mysql-single: the secrets are named <name_prefix>/<key of secrets>."
  type        = string
}

variable "secrets" {
  description = "The secrets, by name: a description and the length of the random value (letters and digits)."
  type = map(object({
    description = string
    length      = number
  }))
}

variable "reader_role_names" {
  description = "IAM roles that may read the secrets, e.g. the database server's role."
  type        = list(string)
  default     = []
}
